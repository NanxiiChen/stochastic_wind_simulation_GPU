from typing import Dict, Tuple, Optional, Union, List
from functools import partial

import jax
import jax.numpy as jnp
import matplotlib.pyplot as plt
from jax import random, vmap

from .simulator import JaxWindSimulator
from .psd import get_spectrum_class


class JaxWindVisualizer:

    def __init__(self, key=0, 
                 simulator: JaxWindSimulator = None, 
                 spectrum_type: str = "kaimal",
                 **kwargs):
        self.key = random.PRNGKey(key)
        self.simulator = simulator if simulator else JaxWindSimulator(key)


    def plot_psd(
        self,
        wind_samples: jnp.ndarray,
        Zs: jnp.ndarray,
        show_num=5,
        save_path: str = None,
        show=True,
        component="u",
        indices: Optional[Union[int, Tuple[int, int]]] = None,
        **kwargs,
    ):
        """绘制模拟结果"""
        n = wind_samples.shape[0]

        if indices is None:
            self.key, subkey = random.split(self.key)
            indices = random.randint(subkey, (show_num,), 0, n)
        elif isinstance(indices, int):
            indices = (indices,)
        elif isinstance(indices, tuple) and len(indices) == 2:
            pass
        else:
            raise ValueError("indices必须是整数或整数序列")

        ncol = kwargs.get("ncol", 3)
        nrow = (len(indices) + ncol - 1) // ncol
        frequencies_theory = self.simulator.calculate_simulation_frequency(
            self.simulator.params["N"], self.simulator.params["dw"]
        )
        S_theory = vmap(
            self.simulator.spectrum.calculate_power_spectrum,
            in_axes=(0, None, None),
        )(frequencies_theory, Zs, component)

        fig, axes = plt.subplots(nrows=nrow, ncols=ncol, figsize=(15, 5 * nrow))
        axes = axes.flatten() if nrow > 1 else [axes]
        for idx, i in enumerate(indices):
            ax = axes[idx]
            data = wind_samples[i]
            frequencies, psd = jax.scipy.signal.welch(
                data,
                fs=1 / self.simulator.params["dt"],
                nperseg=1024,
                scaling="density",
                window="hann",
            )
            ax.loglog(frequencies, psd, label=f"Point {i+1}")

            ax.loglog(
                frequencies_theory,
                S_theory[:, i],
                "--",
                color="black",
                linewidth=2,
                label="Theoretical Reference",
            )
            ax.set(
                xlabel="Frequency (Hz)",
                ylabel="Power Spectral Density (m²/s²)",
                title=f"PSD of {component.upper()} Wind at Point {i+1} (Z={Zs[i]:.2f} m)",
            )

            ax.grid(True, which="both", ls="-", alpha=0.6)
            ax.legend()

        if save_path:
            plt.savefig(save_path)

        if show:
            plt.show()
        else:
            plt.close()

    def _compute_correlation(self, data_i, data_j):
        """计算样本互相关函数 - 使用最大值归一化"""
        data_i_centered = data_i - jnp.mean(data_i)
        data_j_centered = data_j - jnp.mean(data_j)
        
        correlation = jax.scipy.signal.correlate(
            data_i_centered, data_j_centered, mode="full"
        )
        
        # 使用最大值归一化（与理论值保持一致）
        corr_max = jnp.max(jnp.abs(correlation))
        return correlation / (corr_max if corr_max > 0 else 1.0)

    def _calculate_theoretical_correlation(self, S_ii, S_jj, coherence, M):
        """计算理论互相关函数 - 保持最大值归一化"""
        # 原有计算逻辑保持不变
        cross_spectrum = jnp.sqrt(S_ii * S_jj) * coherence

        full_spectrum = jnp.zeros(M, dtype=jnp.complex64)
        N = len(coherence)
        full_spectrum = full_spectrum.at[1 : N + 1].set(cross_spectrum)
        full_spectrum = full_spectrum.at[M - N :].set(
            jnp.flip(jnp.conj(cross_spectrum))
        )

        theo_correlation = jnp.real(jnp.fft.ifft(full_spectrum))
        theo_correlation = jnp.fft.fftshift(theo_correlation)

        theo_max = jnp.max(jnp.abs(theo_correlation))
        return theo_correlation / (theo_max if theo_max > 0 else 1.0)

    def _calculate_theoretical_spectrum(
        self, Z: float, component: str = "u"
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """计算理论功率谱密度"""
        
        u_star = self.simulator.calculate_friction_velocity(
            Z,
            self.simulator.params["U_d"],
            self.simulator.params["z_0"],
            self.simulator.params["z_d"],
            self.simulator.params["K"],
        )
        dw = self.simulator.params["dw"]
        N = self.simulator.params["N"]
        frequencies = self.simulator.calculate_simulation_frequency(N, dw)
        f_nondimensional = self.simulator.calculate_f(
            frequencies, Z, self.simulator.params["U_d"]
        )
        S_theory = (
            self.simulator.calculate_power_spectrum_u(
                frequencies, u_star, f_nondimensional
            )
            if component == "u"
            else self.simulator.calculate_power_spectrum_w(
                frequencies, u_star, f_nondimensional
            )
        )
        return frequencies, S_theory

    def plot_cross_correlation(
        self,
        wind_samples,
        positions,
        wind_speeds,
        save_path=None,
        show=True,
        component="u",
        indices=None,
        downsample=1,
        **kwargs,
    ):
        """绘制互相关函数并与理论值比较（优化版本）"""
        n = wind_samples.shape[0]

        if downsample > 1:
            wind_samples = wind_samples[:, ::downsample]
            dt = self.simulator.params["dt"] * downsample
        else:
            dt = self.simulator.params["dt"]

        self.key, subkey = random.split(self.key)
        if indices is None:
            idx = random.randint(subkey, (1,), 0, n).item()
            indices = idx, idx
        elif isinstance(indices, int):
            indices = (indices, indices)
        elif isinstance(indices, tuple) and len(indices) == 2:
            pass
        else:
            raise ValueError("indices必须是整数或两个整数的元组")

        i, j = indices
        data_i = wind_samples[i]
        data_j = wind_samples[j]

        # 计算实际互相关函数
        correlation = self._compute_correlation(data_i, data_j)
        lags = jnp.arange(-len(data_i) + 1, len(data_i))
        lag_times = lags * dt

        # 提取位置信息
        x_i, y_i, z_i = positions[i]
        x_j, y_j, z_j = positions[j]
        U_zi, U_zj = wind_speeds[i], wind_speeds[j]

        N = self.simulator.params["N"]
        M = self.simulator.params["M"]
        dw = self.simulator.params["dw"]

        frequencies = self.simulator.calculate_simulation_frequency(N, dw)
        S_ii = self.simulator.spectrum.calculate_power_spectrum(
            frequencies, z_i, component
        )
        S_jj = self.simulator.spectrum.calculate_power_spectrum(
            frequencies, z_j, component
        )

        coherence = vmap(
            lambda freq: self.simulator.calculate_coherence(
                x_i,
                x_j,
                y_i,
                y_j,
                z_i,
                z_j,
                2 * jnp.pi * freq,  # 使用角频率
                U_zi,
                U_zj,
                self.simulator.params["C_x"],
                self.simulator.params["C_y"],
                self.simulator.params["C_z"],
            )
        )(frequencies)

        theo_correlation = self._calculate_theoretical_correlation(
            S_ii, S_jj, coherence, M
        )
        theo_lags = jnp.arange(-M // 2, M // 2)
        theo_lag_times = theo_lags * dt

        fig, ax = plt.subplots(figsize=(12, 8))
        mid = len(correlation) // 2
        range_points = kwargs.get("range_points", len(theo_lag_times) // 2)
        plot_corr = correlation[mid - range_points : mid + range_points + 1]
        plot_lag_times = lag_times[mid - range_points : mid + range_points + 1]

        # 截取相同范围的理论相关函数
        theo_mid = len(theo_correlation) // 2
        theo_plot = theo_correlation[
            theo_mid - range_points : theo_mid + range_points + 1
        ]
        theo_plot_times = theo_lag_times[
            theo_mid - range_points : theo_mid + range_points + 1
        ]

        ax.plot(
            plot_lag_times,
            plot_corr,
            label="simulation",
        )
        ax.plot(
            theo_plot_times,
            theo_plot,
            "--",
            color="black",
            linewidth=2,
            label="theoretical",
        )

        plt.title(
            f"Cross Correlation of {component.upper()} Wind at Points {i+1} and {j+1}\n"
        )
        plt.xlabel("Time Lag (s)")
        plt.ylabel("Cross Correlation")
        plt.grid(True)
        plt.legend()

        if save_path:
            plt.savefig(save_path)

        if show:
            plt.show()
        else:
            plt.close()

        return indices
        
    # def plot_cross_coherence(
    #     self,
    #     wind_samples,
    #     positions,
    #     wind_speeds,
    #     save_path=None,
    #     show=True,
    #     component="u",
    #     indices=None,
    #     downsample=1,
    #     **kwargs,
    # ):
    #     """
    #     绘制互相干函数图
    #     """
    #     n = positions.shape[0]
    #     # 处理降采样
    #     if downsample > 1:
    #         wind_samples = wind_samples[:, ::downsample]
    #         dt = self.simulator.params["dt"] * downsample
    #     else:
    #         dt = self.simulator.params["dt"]
    
    #     # 选择要分析的点对
    #     self.key, subkey = random.split(self.key)
    #     if indices is None:
    #         # 如果未指定，随机选择一对点
    #         idx1 = random.randint(subkey, (1,), 0, n).item()
    #         self.key, subkey = random.split(self.key)
    #         idx2 = random.randint(subkey, (1,), 0, n).item()
    #         indices = idx1, idx2
    #     elif isinstance(indices, int):
    #         # 如果是单个索引，选择该点与自身
    #         indices = (indices, indices)
    #     elif isinstance(indices, tuple) and len(indices) == 2:
    #         pass
    #     else:
    #         raise ValueError("indices必须是整数或两个整数的元组")
        
    #     i, j = indices
    #     data_i = wind_samples[i]
    #     data_j = wind_samples[j]
    #     # 计算实测相干函数 - 使用Welch方法
    #     nperseg = kwargs.get("nperseg", min(1024, len(data_i) // 4))
        
    #     # 计算自谱和互谱
    #     fxx, Pxx = jax.scipy.signal.welch(
    #         data_i, fs=1/dt, nperseg=nperseg, 
    #         scaling="density", window="hann",
    #     )
    #     fyy, Pyy = jax.scipy.signal.welch(
    #         data_j, fs=1/dt, nperseg=nperseg, 
    #         scaling="density", window="hann",
    #     )
    #     fxy, Pxy = jax.scipy.signal.csd(
    #         data_i, data_j, fs=1/dt, nperseg=nperseg,
    #         scaling="density", window="hann",
    #     )

    #     measured_coherence = jnp.abs(Pxy)**2 / (Pxx * Pyy +1e-10)

    #     fig, ax = plt.subplots(figsize=(12, 8))
    #     ax.loglog(fxx, Pxx, label=f"PSD of Point {i+1}")
    #     ax.loglog(fyy, Pyy, label=f"PSD of Point {j+1}")
    #     ax.loglog(fxy, Pxy, label=f"CSD of Points {i+1} and {j+1}")
    #     ax.set(
    #         title=f"Power Spectral Density and Cross Spectral Density of {component.upper()} Wind at Points {i+1} and {j+1}",
    #         xlabel="Frequency (Hz)",
    #         ylabel="Power Spectral Density (m²/s²)",
    #     )
    #     plt.show(block=False)


    #     x_i, y_i, z_i = positions[i]
    #     x_j, y_j, z_j = positions[j]
    #     U_zi, U_zj = wind_speeds[i], wind_speeds[j]
    #     theoretical_coherence = vmap(
    #         lambda freq: self.simulator.calculate_coherence(
    #             x_i, x_j, y_i, y_j, z_i, z_j,
    #             2 * jnp.pi * freq, U_zi, U_zj,
    #             self.simulator.params["C_x"], self.simulator.params["C_y"], self.simulator.params["C_z"]
    #         )
    #     )(fxx)

    #     fig, ax = plt.subplots(figsize=(12, 8))
    #     ax.semilogy(fxy, measured_coherence, label="Measured Coherence")
    #     ax.semilogy(fxx, theoretical_coherence, '--', color='black', linewidth=2, label="Theoretical Coherence")
    #     ax.set(
    #         title=f"Cross Coherence of {component.upper()} Wind at Points {i+1} and {j+1}",
    #         xlabel="Frequency (Hz)",
    #         ylabel="Coherence",
    #     )
    #     ax.grid(True, which="both", ls="-", alpha=0.6)
    #     ax.legend()
    #     if save_path:
    #         plt.savefig(save_path)
    #     if show:
    #         plt.show()
    #     else:
    #         plt.close()

    #     return indices
