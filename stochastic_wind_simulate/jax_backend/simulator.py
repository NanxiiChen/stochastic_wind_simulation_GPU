from functools import partial
from typing import Dict

import jax.numpy as jnp
from jax import jit, random, vmap
from jax.scipy.linalg import cholesky


class JaxWindSimulator:
    """随机风场模拟器类"""

    def __init__(self, key=0):
        """
        初始化风场模拟器

        参数:
        key - JAX随机数种子
        """
        self.key = random.PRNGKey(key)
        self.params = self._set_default_parameters()

    def _set_default_parameters(self) -> Dict:
        """设置默认风场模拟参数"""
        params = {
            "K": 0.4,  # 无量纲常数
            "H_bar": 10.0,  # 周围建筑物平均高度(m)
            "z_0": 0.05,  # 地表粗糙高度
            "C_x": 16.0,  # x方向衰减系数
            "C_y": 6.0,  # y方向衰减系数
            "C_z": 10.0,  # z方向衰减系数
            "w_up": 5.0,  # 截止频率(Hz)
            "N": 3000,  # 频率分段数
            "M": 6000,  # 时间点数(M=2N)
            "T": 600,  # 模拟时长(s)
            "dt": 0.1,  # 时间步长(s)
            "U_d": 25.0,  # 设计基本风速(m/s)
        }
        params["dw"] = params["w_up"] / params["N"]  # 频率增量
        params["z_d"] = params["H_bar"] - params["z_0"] / params["K"]  # 计算零平面位移

        return params

    def update_parameters(self, **kwargs):
        """更新模拟参数"""
        for key, value in kwargs.items():
            if key in self.params:
                self.params[key] = value

        # 更新依赖参数
        self.params["dw"] = self.params["w_up"] / self.params["N"]
        self.params["z_d"] = (
            self.params["H_bar"] - self.params["z_0"] / self.params["K"]
        )

    @staticmethod
    @jit
    def calculate_friction_velocity(Z, U_d, z_0, z_d, K):
        """计算风的摩阻速度 u_*"""
        return K * U_d / jnp.log((Z - z_d) / z_0)

    @staticmethod
    @jit
    def calculate_f(n, Z, U_d):
        """计算无量纲频率 f"""
        return n * Z / U_d

    @staticmethod
    @jit
    def calculate_power_spectrum_u(n, u_star, f):
        """计算顺风向脉动风功率谱密度 S_u(n)"""
        return (u_star**2 / n) * (200 * f / ((1 + 50 * f) ** (5 / 3)))

    @staticmethod
    @jit
    def calculate_power_spectrum_w(n, u_star, f):
        """计算竖向脉动风功率谱密度 S_w(n)"""
        return (u_star**2 / n) * (6 * f / ((1 + 4 * f) ** 2))

    @staticmethod
    @jit
    def calculate_coherence(x_i, x_j, y_i, y_j, z_i, z_j, w, U_zi, U_zj, C_x, C_y, C_z):
        """计算空间相关函数 Coh"""
        distance_term = jnp.sqrt(
            C_x**2 * (x_i - x_j) ** 2
            + C_y**2 * (y_i - y_j) ** 2
            + C_z**2 * (z_i - z_j) ** 2
        )
        # 增加数值稳定性保护，避免除以接近零的值
        denominator = 2 * jnp.pi * (U_zi + U_zj)
        safe_denominator = jnp.maximum(denominator, 1e-8)  # 设置安全最小值

        return jnp.exp(-2 * w * distance_term / safe_denominator)

    @staticmethod
    @jit
    def calculate_cross_spectrum(S_ii, S_jj, coherence):
        """计算互谱密度函数 S_ij"""
        return jnp.sqrt(S_ii * S_jj) * coherence

    @staticmethod
    def calculate_simulation_frequency(N, dw):
        """计算模拟频率数组"""
        # return jnp.array([(l - 0.5) * dw for l in range(1, N + 1)])
        return jnp.arange(1, N + 1) * dw - dw / 2
    
    @partial(jit, static_argnums=(0,3))
    def calculate_spectrum_for_position(self, freq, positions, spectrum_func):
        u_stars = self.calculate_friction_velocity(
            positions[:, 2],
            self.params["U_d"],
            self.params["z_0"],
            self.params["z_d"],
            self.params["K"],
        )
        f_values = self.calculate_f(freq, positions[:, 2], self.params["U_d"])
        S_values = spectrum_func(freq, u_stars, f_values)
        return S_values


    def build_spectrum_matrix(self, positions, wind_speeds, frequencies, spectrum_func):
        """构建互谱密度矩阵 S(w)"""
        n = positions.shape[0]

        x_i = jnp.expand_dims(positions[:, 0], 1).repeat(n, axis=1)  # [n, n]
        x_j = jnp.expand_dims(positions[:, 0], 0).repeat(n, axis=0)  # [n, n]
        y_i = jnp.expand_dims(positions[:, 1], 1).repeat(n, axis=1)  # [n, n]
        y_j = jnp.expand_dims(positions[:, 1], 0).repeat(n, axis=0)  # [n, n]
        z_i = jnp.expand_dims(positions[:, 2], 1).repeat(n, axis=1)  # [n, n]
        z_j = jnp.expand_dims(positions[:, 2], 0).repeat(n, axis=0)  # [n, n]

        U_i = jnp.expand_dims(wind_speeds, 1).repeat(n, axis=1)  # [n, n]
        U_j = jnp.expand_dims(wind_speeds, 0).repeat(n, axis=0)  # [n, n]

        @partial(jit, static_argnums=(2,))
        def _build_spectrum_for_position(freq, positions, spectrum_func):
            s_values = self.calculate_spectrum_for_position(
                freq, positions, spectrum_func
            )
            s_i = s_values.reshape(n, 1)  # [n, 1]
            s_j = s_values.reshape(1, n)  # [1, n]
            coherence = self.calculate_coherence(
                x_i, x_j, y_i, y_j, z_i, z_j, freq, U_i, U_j,
                self.params["C_x"], self.params["C_y"], self.params["C_z"]
            )
            cross_spectrum = self.calculate_cross_spectrum(s_i, s_j, coherence)
            return cross_spectrum
        
        # 对每个频率点并行计算互谱密度矩阵
        S_matrices = vmap(
            _build_spectrum_for_position,
            in_axes=(0, None, None),
        )(frequencies, positions, spectrum_func)
        
        return S_matrices

    def simulate_wind(self, positions, wind_speeds, direction="u"):
        """
        模拟脉动风场

        参数:
        positions - 形状为(n, 3)的数组，每行为(x, y, z)坐标
        wind_speeds - 形状为(n,)的数组，表示各点的平均风速
        direction - 风向，'u'表示顺风向，'w'表示竖向

        返回:
        wind_samples - 形状为(n, M)的数组，表示各点的脉动风时程
        frequencies - 频率数组
        """
        self.key, subkey = random.split(self.key)
        if not isinstance(positions, jnp.ndarray):
            positions = jnp.array(positions)

        return self._simulate_fluctuating_wind(
            positions, wind_speeds, subkey, direction
        )

    def _simulate_fluctuating_wind(self, positions, wind_speeds, key, direction):
        """风场模拟的内部实现"""
        n = positions.shape[0]
        N = self.params["N"]
        M = self.params["M"]
        dw = self.params["dw"]

        frequencies = self.calculate_simulation_frequency(N, dw)
        spectrum_func = (
            self.calculate_power_spectrum_u
            if direction == "u"
            else self.calculate_power_spectrum_w
        )

        # 构建互谱密度矩阵
        S_matrices = self.build_spectrum_matrix(
            positions, wind_speeds, frequencies, spectrum_func
        )

        # 对每个频率点进行Cholesky分解
        @jit
        def cholesky_with_reg(S):
            return cholesky(S + jnp.eye(n) * 1e-12, lower=True)

        H_matrices = vmap(cholesky_with_reg)(S_matrices)

        # 生成随机相位
        key, subkey = random.split(key)
        phi = random.uniform(subkey, (n, n, N), minval=0, maxval=2 * jnp.pi)

        @partial(jit, static_argnums=(1, 2, 3))
        def compute_B_for_point(j, N, M, n, H_matrices, phi):
            # 创建掩码
            row_indices = jnp.arange(n)
            mask = row_indices <= j
            H_terms = H_matrices[:, j, :]
            H_masked = H_terms * mask

            # 转换相位到正确形状
            phi_masked = phi[j, :, :] * mask.reshape(n, 1)
            exp_terms = jnp.exp(1j * phi_masked.transpose(1, 0))

            B_values = jnp.einsum("nl,nl->n", H_masked.reshape(N, n), exp_terms * mask)

            return jnp.pad(B_values, (0, M - N), mode="constant")

        # 对每个点并行计算B
        B = vmap(compute_B_for_point, in_axes=(0, None, None, None, None, None))(
            jnp.arange(n), N, M, n, H_matrices, phi
        )
        G = vmap(jit(jnp.fft.ifft))(B)*M

        # 计算风场样本
        @jit
        def compute_samples_for_point(j):
            p_indices = jnp.arange(M)
            exponent = jnp.exp(1j * (p_indices * jnp.pi / M))
            terms = G[j] * exponent
            # return 2 * jnp.sqrt(dw / (2 * jnp.pi)) * jnp.real(terms)
            # return 2 * jnp.sqrt(2) * jnp.sqrt(dw / (2 * jnp.pi)) * jnp.real(terms)
            return jnp.sqrt(2 * dw) * jnp.real(terms)


        wind_samples = vmap(compute_samples_for_point)(jnp.arange(n))

        return wind_samples, frequencies
