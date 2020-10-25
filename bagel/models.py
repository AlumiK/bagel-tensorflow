import bagel
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from typing import Sequence, Tuple, Optional


class AutoencoderLayer(tf.keras.layers.Layer):

    def __init__(self, output_dim: int, hidden_dims: Sequence[int]):
        super().__init__()
        self._hidden = tf.keras.Sequential()
        for hidden_dim in hidden_dims:
            self._hidden.add(
                tf.keras.layers.Dense(hidden_dim, activation='relu', kernel_regularizer=tf.keras.regularizers.L2(0.001))
            )
        self._mean = tf.keras.layers.Dense(output_dim, kernel_regularizer=tf.keras.regularizers.L2(0.001))
        self._std = tf.keras.layers.Dense(output_dim, kernel_regularizer=tf.keras.regularizers.L2(0.001))

    def call(self, inputs, **kwargs):
        x = self._hidden(inputs)
        mean = self._mean(x)
        std = tf.math.softplus(self._std(x)) + 1e-6
        return mean, std


class ConditionalVariationalAutoencoder(tf.keras.Model):

    def __init__(self, encoder: AutoencoderLayer, decoder: AutoencoderLayer):
        super().__init__()
        self._encoder = encoder
        self._decoder = decoder

    def call(self, inputs, **kwargs):
        x, y = tuple(inputs)
        n_samples = kwargs.get('n_samples', 1)
        concatted = tf.keras.layers.Concatenate()([x, y])
        z_mean, z_std = self._encoder(concatted)
        q_zx = tfp.distributions.Normal(z_mean, z_std)
        p_z = tfp.distributions.Normal(tf.zeros(z_mean.shape), tf.ones(z_std.shape))
        z = p_z.sample((n_samples,)) * tf.expand_dims(z_std, 0) + tf.expand_dims(z_mean, 0)
        y = tf.broadcast_to(y, [n_samples, y.shape[0], y.shape[1]])
        concatted = tf.keras.layers.Concatenate()([z, y])
        x_mean, x_std = self._decoder(concatted)
        p_xz = tfp.distributions.Normal(x_mean, x_std)
        return q_zx, p_xz, z

    def get_config(self):
        raise NotImplementedError


class Bagel:

    def __init__(self,
                 window_size: int = 120,
                 hidden_dims: Optional[Sequence] = None,
                 latent_dim: int = 8,
                 dropout_rate: float = 0.1):
        super().__init__()
        self._hidden_dims = [100, 100] if hidden_dims is None else hidden_dims
        self._latent_dim = latent_dim
        self._window_size = window_size
        self._cond_size = 60 + 24 + 7
        self._dropout_rate = dropout_rate
        self._model = ConditionalVariationalAutoencoder(
            encoder=AutoencoderLayer(
                output_dim=self._latent_dim,
                hidden_dims=self._hidden_dims
            ),
            decoder=AutoencoderLayer(
                output_dim=self._window_size,
                hidden_dims=self._hidden_dims
            ),
        )
        self._p_z = tfp.distributions.Normal(tf.zeros(self._latent_dim), tf.ones(self._latent_dim))
        self._lr_scheduler = tf.keras.optimizers.schedules.ExponentialDecay(
            initial_learning_rate=1e-3,
            decay_steps=1000,
            decay_rate=0.75,
            staircase=True
        )
        self._optimizer = tf.keras.optimizers.Adam(learning_rate=self._lr_scheduler, clipnorm=10.)

    @staticmethod
    def _m_elbo(x: tf.Tensor,
                z: tf.Tensor,
                normal: tf.Tensor,
                p_xz: tfp.distributions.Normal,
                q_zx: tfp.distributions.Normal,
                p_z: tfp.distributions.Normal) -> tf.Tensor:
        x = tf.expand_dims(x, 0)
        normal = tf.expand_dims(normal, 0)
        log_p_xz = p_xz.log_prob(x)
        log_q_zx = tf.math.reduce_sum(q_zx.log_prob(z), axis=-1)
        log_p_z = tf.math.reduce_sum(p_z.log_prob(z), axis=-1)
        ratio = (tf.math.reduce_sum(normal, axis=-1) / float(normal.shape[-1]))
        return tf.math.reduce_mean(tf.math.reduce_sum(log_p_xz * normal, axis=-1) + log_p_z * ratio - log_q_zx)

    def _missing_imputation(self, x: tf.Tensor, y: tf.Tensor, normal: tf.Tensor, max_iter: int = 10) -> tf.Tensor:
        cond = tf.cast(normal, 'bool')
        for _ in range(max_iter):
            _, p_xz, _ = self._model([x, y])
            reconstruction = p_xz.sample()[0]
            x = tf.where(cond, x, reconstruction)
        return x

    @tf.function
    def _train_step(self, x: tf.Tensor, y: tf.Tensor, normal: tf.Tensor) -> tf.Tensor:
        with tf.GradientTape() as tape:
            y = tf.keras.layers.Dropout(self._dropout_rate)(y)
            q_zx, p_xz, z = self._model([x, y])
            loss = -self._m_elbo(x, z, normal, p_xz, q_zx, self._p_z)
            loss += tf.math.add_n(self._model.losses)
        grads = tape.gradient(loss, self._model.trainable_weights)
        self._optimizer.apply_gradients(zip(grads, self._model.trainable_weights))
        return loss

    @tf.function
    def _validation_step(self, x: tf.Tensor, y: tf.Tensor, normal: tf.Tensor) -> tf.Tensor:
        q_zx, p_xz, z = self._model([x, y])
        val_loss = -self._m_elbo(x, z, normal, p_xz, q_zx, self._p_z)
        val_loss += tf.math.add_n(self._model.losses)
        return val_loss

    @tf.function
    def _test_step(self, x: tf.Tensor, y: tf.Tensor, normal: tf.Tensor) -> Tuple[tf.Tensor, np.ndarray]:
        x = self._missing_imputation(x, y, normal)
        q_zx, p_xz, z = self._model([x, y], n_samples=128)
        test_loss = -self._m_elbo(x, z, normal, p_xz, q_zx, self._p_z)
        log_p_xz = p_xz.log_prob(x)
        return test_loss, log_p_xz

    def fit(self,
            kpi: bagel.data.KPI,
            epochs: int,
            validation_kpi: Optional[bagel.data.KPI] = None,
            batch_size: int = 256):
        dataset = bagel.data.KPIDataset(kpi, window_size=self._window_size, missing_injection_rate=0.01).to_tensorflow()
        dataset = dataset.shuffle(len(dataset)).batch(batch_size, drop_remainder=True)
        validation_dataset = None
        if validation_kpi is not None:
            validation_dataset = bagel.data.KPIDataset(validation_kpi, window_size=self._window_size).to_tensorflow()
            validation_dataset = validation_dataset.shuffle(len(validation_dataset)).batch(batch_size)
        for epoch in range(epochs):
            print(f'Training Epoch: {epoch + 1}/{epochs}')
            progbar = tf.keras.utils.Progbar(len(dataset) + (0 if validation_kpi is None else len(validation_dataset)),
                                             interval=0.5)
            for batch in dataset:
                y, x, normal = batch
                loss = self._train_step(x, y, normal)
                progbar.add(1, values=[('loss', loss)])
            if validation_kpi is not None:
                for batch in validation_dataset:
                    y, x, normal = batch
                    val_loss = self._validation_step(x, y, normal)
                    progbar.add(1, values=[('val_loss', val_loss)])

    def predict(self, kpi: bagel.data.KPI, batch_size: int = 256) -> np.ndarray:
        print('Testing Epoch')
        kpi = kpi.no_labels()
        dataset = bagel.data.KPIDataset(kpi, window_size=self._window_size).to_tensorflow()
        dataset = dataset.batch(batch_size)
        progbar = tf.keras.utils.Progbar(len(dataset), interval=0.5)
        anomaly_scores = []
        for batch in dataset:
            y, x, normal = batch
            test_loss, log_p_xz = self._test_step(x, y, normal)
            anomaly_scores.extend(-np.mean(log_p_xz[:, :, -1], axis=0))
            progbar.add(1, values=[('test_loss', test_loss)])
        anomaly_scores = np.asarray(anomaly_scores, dtype=np.float32)
        return np.concatenate([np.ones(self._window_size - 1) * np.min(anomaly_scores), anomaly_scores])
