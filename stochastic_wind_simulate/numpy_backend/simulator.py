import numpy as np
from typing import Dict, Tuple
from scipy.linalg import cholesky
from .psd import get_spectrum_class
from ..base_simulator import BaseWindSimulator


class NumpyWindSimulator(BaseWindSimulator):
    """
    Stochastic wind field simulator class implemented using NumPy.
    
    This class provides functionality for simulating fluctuating wind fields using
    the spectral representation method with automatic batching for memory management.
    NumPy backend uses CPU computations and is suitable for moderate-scale simulations.
    """

    def __init__(self, key=0, spectrum_type="kaimal-nd"):
        """
        Initialize the wind field simulator.
        
        Args:
            key (int): Random number seed for reproducible results
            spectrum_type (str): Type of wind spectrum to use (default: "kaimal-nd")
        """
        super().__init__()  # Initialize base class
        self.seed = key
        np.random.seed(key)
        self.spectrum = get_spectrum_class(spectrum_type)(**self.params)

    def _set_default_parameters(self) -> Dict:
        """
        Set default wind field simulation parameters.
        
        Returns:
            Dict: Dictionary containing default simulation parameters including
                 physical constants, grid specifications, and numerical settings
        """
        params = {
            "K": 0.4,  # Dimensionless constant
            "H_bar": 10.0,  # Average height of surrounding buildings (m)
            "z_0": 0.05,  # Surface roughness height
            "alpha_0": 0.16,  # Surface roughness exponent
            "C_x": 16.0,  # Decay coefficient in x direction
            "C_y": 6.0,  # Decay coefficient in y direction
            "C_z": 10.0,  # Decay coefficient in z direction
            "w_up": 5.0,  # Cutoff frequency (Hz)
            "N": 3000,  # Number of frequency segments
            "M": 6000,  # Number of time points (M=2N)
            "T": 600,  # Simulation duration (s)
            "dt": 0.1,  # Time step (s)
            "U_d": 25.0,  # Design basic wind speed (m/s)
        }
        params["dw"] = params["w_up"] / params["N"]  # Frequency increment
        params["z_d"] = params["H_bar"] - params["z_0"] / params["K"]  # Calculate zero plane displacement

        return params

    def update_parameters(self, **kwargs):
        """Update simulation parameters."""
        for key, value in kwargs.items():
            if key in self.params:
                self.params[key] = value

        # Update dependent parameters
        self.params["dw"] = self.params["w_up"] / self.params["N"]
        self.params["z_d"] = (
            self.params["H_bar"] - self.params["z_0"] / self.params["K"]
        )
        self.spectrum.params = self.params  # Update spectrum parameters

    @staticmethod
    def calculate_coherence(x_i, x_j, y_i, y_j, z_i, z_j, w, U_zi, U_zj, C_x, C_y, C_z):
        """Calculate spatial correlation function Coh."""
        distance_term = np.sqrt(
            C_x**2 * (x_i - x_j) ** 2
            + C_y**2 * (y_i - y_j) ** 2
            + C_z**2 * (z_i - z_j) ** 2
        )
        # Add numerical stability protection to avoid division by near-zero values
        denominator = 2 * np.pi * (U_zi + U_zj)
        safe_denominator = np.maximum(denominator, 1e-8)  # Set safe minimum value

        return np.exp(-2 * w * distance_term / safe_denominator)

    @staticmethod
    def calculate_cross_spectrum(S_ii, S_jj, coherence):
        """Calculate cross-spectral density function S_ij."""
        return np.sqrt(S_ii * S_jj) * coherence

    @staticmethod
    def calculate_simulation_frequency(N, dw):
        """Calculate simulation frequency array."""
        return np.arange(1, N + 1) * dw - dw / 2

    def build_spectrum_matrix(self, positions, wind_speeds, frequencies, component, **kwargs):
        """Build cross-spectral density matrix S(w) using the new spectrum interface."""
        n = positions.shape[0]
        num_freqs = len(frequencies)

        def _build_spectrum_for_position(freq, positions, component, **kwargs):
            s_values = self.spectrum.calculate_power_spectrum(
                freq, positions[:, 2], component, **kwargs
            )
            s_i = s_values.reshape(n, 1)  # [n, 1]
            s_j = s_values.reshape(1, n)  # [1, n]
            
            # Create grid coordinates
            x_i = positions[:, 0][:, np.newaxis].repeat(n, axis=1)  # [n, n]
            x_j = positions[:, 0][np.newaxis, :].repeat(n, axis=0)  # [n, n]
            y_i = positions[:, 1][:, np.newaxis].repeat(n, axis=1)  # [n, n]
            y_j = positions[:, 1][np.newaxis, :].repeat(n, axis=0)  # [n, n]
            z_i = positions[:, 2][:, np.newaxis].repeat(n, axis=1)  # [n, n]
            z_j = positions[:, 2][np.newaxis, :].repeat(n, axis=0)  # [n, n]
            U_i = wind_speeds[:, np.newaxis].repeat(n, axis=1)  # [n, n]
            U_j = wind_speeds[np.newaxis, :].repeat(n, axis=0)  # [n, n]
            
            coherence = self.calculate_coherence(
                x_i, x_j, y_i, y_j, z_i, z_j, freq, U_i, U_j,
                self.params["C_x"], self.params["C_y"], self.params["C_z"]
            )
            cross_spectrum = self.calculate_cross_spectrum(s_i, s_j, coherence)
            return cross_spectrum
        
        # Compute cross-spectral density matrix for each frequency point
        S_matrices = np.array([
            _build_spectrum_for_position(freq, positions, component, **kwargs)
            for freq in frequencies
        ])
        
        return S_matrices

    def simulate_wind(self, positions, wind_speeds, component="u", 
                     max_memory_gb=8.0, point_batch_size=None, 
                     freq_batch_size=None, auto_batch=True, **kwargs):
        """
        Simulate fluctuating wind field with automatic batching for memory management.
        
        NumPy backend uses CPU memory, so we typically have more memory available
        compared to GPU backends, but batching is still useful for very large problems.
        
        Args:
            positions: Array of shape (n, 3), each row represents (x, y, z) coordinates
            wind_speeds: Array of shape (n,), represents mean wind speed at each point
            component: Wind component, 'u' for along-wind, 'w' for vertical
            max_memory_gb: Maximum memory limit in GB (default: 8.0, higher for CPU)
            point_batch_size: Manual point batch size (auto-calculate if None)
            freq_batch_size: Manual frequency batch size (auto-calculate if None)
            auto_batch: If True, automatically determine if batching is needed
            
        Returns:
            wind_samples: Array of shape (n, M), fluctuating wind time series at each point
            frequencies: Frequency array
        """
        np.random.seed(self.seed)
        self.seed += 1
        
        # Convert inputs to NumPy arrays
        positions = np.asarray(positions, dtype=np.float64)
        wind_speeds = np.asarray(wind_speeds, dtype=np.float64)
        
        n = positions.shape[0]
        N = self.params["N"]
        
        # Use base class method to determine batching strategy
        use_batching, point_batch_size, freq_batch_size = self._should_use_batching(
            n, N, max_memory_gb, point_batch_size, freq_batch_size, auto_batch
        )
        
        # Print information about memory and batching decisions
        estimated_memory = self.estimate_memory_requirement(n, N)
        if use_batching:
            n_point_batches = self._get_batch_info(n, point_batch_size)
            n_freq_batches = self._get_batch_info(N, freq_batch_size)
            self.print_batch_info(
                estimated_memory, max_memory_gb, use_batching, 
                point_batch_size, freq_batch_size, n_point_batches, n_freq_batches
            )
        else:
            self.print_batch_info(estimated_memory, max_memory_gb, use_batching)
        
        if use_batching:
            return self._simulate_wind_with_batching(
                positions, wind_speeds, component, 
                point_batch_size, freq_batch_size, **kwargs
            )
        else:
            # Use the direct method for small problems
            return self._simulate_fluctuating_wind(
                positions, wind_speeds, component, **kwargs
            )

    def _simulate_fluctuating_wind(self, positions, wind_speeds, component, **kwargs):
        """Internal implementation of wind field simulation - corresponding to JAX version."""
        n = positions.shape[0]
        N = self.params["N"]
        M = self.params["M"]
        dw = self.params["dw"]

        # Calculate frequency
        frequencies = self.calculate_simulation_frequency(N, dw)

        # Build cross-spectral density matrix
        S_matrices = self.build_spectrum_matrix(
            positions, wind_speeds, frequencies, component, **kwargs
        )

        # Perform Cholesky decomposition for each frequency point
        H_matrices = np.zeros((N, n, n), dtype=np.complex128)
        for i in range(N):
            # Add small diagonal terms to improve numerical stability
            S_reg = S_matrices[i] + np.eye(n) * 1e-12
            H_matrices[i] = cholesky(S_reg, lower=True)

        # Generate random phases - same as JAX version
        phi = np.random.uniform(0, 2*np.pi, (n, N))

        # Calculate B matrix - corrected version, consistent with JAX version
        B = np.zeros((n, M), dtype=np.complex128)

        for j in range(n):
            # Create mask matrix where mask[m] = True if m <= j
            m_indices = np.arange(n)  # [n,]
            mask = m_indices <= j  # [n,] boolean mask
            
            # H_matrices[l, j, m] for all frequencies l of H_{jm}
            H_jm_all = H_matrices[:, j, :]  # [N, n]
            
            # phi[m, l] -> phi.T to get [N, n]
            phi_transposed = phi.T  # [N, n]
            
            # Calculate exp(i * phi_{ml})
            exp_terms = np.exp(1j * phi_transposed)  # [N, n]
            
            # Apply mask and sum
            # Broadcast mask to [N, n] shape
            mask_expanded = np.broadcast_to(mask, (N, n))  # [N, n]
            masked_terms = np.where(mask_expanded, H_jm_all * exp_terms, 0.0)  # [N, n]
            B_values = np.sum(masked_terms, axis=1)  # [N,]
            
            # Put B_values into the first N positions of B matrix, remaining positions stay 0
            B[j, :N] = B_values
        
        # FFT transform
        G = np.fft.ifft(B) * M
        
        # Calculate wind field samples
        wind_samples = np.zeros((n, M))
        p_indices = np.arange(M)
        exp_factor = np.exp(1j * (p_indices * np.pi / M))
        
        for j in range(n):
            wind_samples[j] = np.sqrt(2 * dw) * np.real(G[j] * exp_factor)
        
        return wind_samples, frequencies

    def _simulate_wind_with_batching(self, positions, wind_speeds, component,
                                   point_batch_size, freq_batch_size, **kwargs):
        """Internal implementation of batched wind field simulation."""
        n = positions.shape[0]
        N = self.params["N"]
        M = self.params["M"]
        dw = self.params["dw"]
        
        # Use base class methods for batch calculations
        n_point_batches = self._get_batch_info(n, point_batch_size)
        n_freq_batches = self._get_batch_info(N, freq_batch_size)
        
        frequencies = self.calculate_simulation_frequency(N, dw)
        
        # Initialize result array
        wind_samples = np.zeros((n, M))
        
        # Process in batches
        for point_batch_idx in range(n_point_batches):
            start_point, end_point = self._get_batch_range(point_batch_idx, point_batch_size, n)
            
            batch_positions = positions[start_point:end_point]
            batch_wind_speeds = wind_speeds[start_point:end_point]
            
            # Use base class method for progress reporting
            self.print_batch_progress(point_batch_idx, n_point_batches, "point", start_point, end_point)
            
            # Process this point batch
            batch_samples = self._simulate_point_batch(
                batch_positions, batch_wind_speeds, component, 
                freq_batch_size, frequencies, **kwargs
            )
            
            wind_samples[start_point:end_point] = batch_samples
        
        return wind_samples, frequencies
    
    def _simulate_point_batch(self, positions, wind_speeds, component, 
                            freq_batch_size, frequencies, **kwargs):
        """Simulate a batch of points, potentially with frequency batching."""        
        n = positions.shape[0]
        N = self.params["N"]
        M = self.params["M"]
        dw = self.params["dw"]
        
        if freq_batch_size >= N:
            # No frequency batching needed, use direct simulation
            return self._simulate_fluctuating_wind(positions, wind_speeds, component, **kwargs)[0]
        
        # Build spectrum matrices in frequency batches
        n_freq_batches = self._get_batch_info(N, freq_batch_size)
        S_matrices_full = np.zeros((N, n, n))
        
        for freq_batch_idx in range(n_freq_batches):
            start_freq, end_freq = self._get_batch_range(freq_batch_idx, freq_batch_size, N)
            
            batch_frequencies = frequencies[start_freq:end_freq]
            
            # Build spectrum matrix for this frequency batch
            S_batch = self.build_spectrum_matrix(
                positions, wind_speeds, batch_frequencies, component, **kwargs
            )
            
            S_matrices_full[start_freq:end_freq] = S_batch
        
        # Process the full spectrum for this point batch
        return self._process_spectrum_to_samples(S_matrices_full, n, N, M, dw)
    
    def _process_spectrum_to_samples(self, S_matrices, n, N, M, dw):
        """Process spectrum matrices to wind samples (extracted from main simulation)."""
        # Perform Cholesky decomposition for each frequency point
        H_matrices = np.zeros((N, n, n), dtype=np.complex128)
        for i in range(N):
            # Add small diagonal terms to improve numerical stability
            S_reg = S_matrices[i] + np.eye(n) * 1e-12
            H_matrices[i] = cholesky(S_reg, lower=True)

        # Generate random phases - use current random state for consistency
        phi = np.random.uniform(0, 2*np.pi, (n, N))

        # Calculate B matrix - consistent with other backends
        B = np.zeros((n, M), dtype=np.complex128)

        for j in range(n):
            # Create mask matrix where mask[m] = True if m <= j
            m_indices = np.arange(n)  # [n,]
            mask = m_indices <= j  # [n,] boolean mask
            
            # H_matrices[l, j, m] for all frequencies l of H_{jm}
            H_jm_all = H_matrices[:, j, :]  # [N, n]
            
            # phi[m, l] -> phi.T to get [N, n]
            phi_transposed = phi.T  # [N, n]
            
            # Calculate exp(i * phi_{ml})
            exp_terms = np.exp(1j * phi_transposed)  # [N, n]
            
            # Apply mask and sum
            # Broadcast mask to [N, n] shape
            mask_expanded = np.broadcast_to(mask, (N, n))  # [N, n]
            masked_terms = np.where(mask_expanded, H_jm_all * exp_terms, 0.0)  # [N, n]
            B_values = np.sum(masked_terms, axis=1)  # [N,]
            
            # Put B_values into the first N positions of B matrix, remaining positions stay 0
            B[j, :N] = B_values
        
        # FFT transform
        G = np.fft.ifft(B, axis=1) * M
        
        # Calculate wind field samples
        wind_samples = np.zeros((n, M))
        p_indices = np.arange(M)
        exp_factor = np.exp(1j * (p_indices * np.pi / M))
        
        for j in range(n):
            wind_samples[j] = np.sqrt(2 * dw) * np.real(G[j] * exp_factor)
        
        return wind_samples

    def estimate_memory_requirement(self, n_points, n_frequencies):
        """
        Estimate memory requirement for NumPy backend in GB.
        
        NumPy uses CPU memory and typically 64-bit floats, so we adjust the estimation.
        
        Args:
            n_points: Number of simulation points
            n_frequencies: Number of frequency points
            
        Returns:
            Estimated memory requirement in GB
        """
        # NumPy typically uses 64-bit floats (8 bytes) instead of 32-bit
        dtype_size = 8
        
        S_memory = n_frequencies * n_points * n_points * dtype_size  # Real
        H_memory = n_frequencies * n_points * n_points * dtype_size * 2  # Complex
        B_memory = n_points * (n_frequencies * 2) * dtype_size * 2  # Complex, M = 2*N
        
        # CPU memory management is different, use a smaller safety factor
        total_bytes = (S_memory + H_memory + B_memory) * 1.5
        
        return total_bytes / (1024**3)  # Convert to GB