# This file is part of pi-stomp.
#
# pi-stomp is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pi-stomp is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pi-stomp.  If not, see <https://www.gnu.org/licenses/>.

import logging
import math
import os
import threading
import time

import numpy as np

try:
    import jack
    JACK_AVAILABLE = True
except ImportError:
    JACK_AVAILABLE = False
    logging.warning("python-jack-client not available; tuner will not function")

# Musical note names (chromatic scale starting at C)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# A4 reference frequency
A4_FREQ = 440.0


def _freq_to_note(frequency):
    """Convert a frequency in Hz to (note_name, octave, cents_offset).

    Returns (None, None, 0.0) if frequency is out of range or zero.
    """
    if frequency <= 0:
        return None, None, 0.0

    # Number of semitones from A4
    semitones_from_a4 = 12.0 * math.log2(frequency / A4_FREQ)
    nearest_semitone = round(semitones_from_a4)
    cents = (semitones_from_a4 - nearest_semitone) * 100.0

    # A4 is MIDI note 69, which is index 9 in our NOTE_NAMES (A is at index 9)
    # note_index relative to C0: A4 = 12*4 + 9 = 57 semitones above C0
    midi_note = 69 + nearest_semitone
    if midi_note < 0 or midi_note > 127:
        return None, None, 0.0

    note_name = NOTE_NAMES[midi_note % 12]
    octave = (midi_note // 12) - 1  # MIDI octave convention: C4 = midi 60

    return note_name, octave, cents


def _yin_pitch(signal, sample_rate, threshold=0.15):
    """YIN pitch detection with FFT-accelerated difference function.

    Uses FFT cross-correlation to compute the difference function in O(n log n)
    instead of the naive O(n²) loop. This enables larger buffer sizes and faster
    processing on the Pi, both of which improve pitch accuracy.

    Returns (frequency, confidence) or (0.0, 0.0) if no pitch detected.

    Based on: De Cheveigné, A. & Kawahara, H. (2002).
    "YIN, a fundamental frequency estimator for speech and music."
    """
    N = len(signal)
    W = N // 2

    # --- Difference function via FFT ---
    # d(τ) = Σ_{j=0}^{W-1} (x[j] - x[j+τ])²
    #       = Σ x[j]² + Σ x[j+τ]² - 2·Σ x[j]·x[j+τ]

    # Cumulative sum of squared samples (prepend 0 for easy range sums)
    x_sq_cs = np.concatenate(([0.0], np.cumsum(signal * signal)))

    # Term 1: energy of first window x[0..W-1] (constant for all τ)
    term1 = x_sq_cs[W]

    # Term 2: energy of shifted window x[τ..τ+W-1] for each τ in [0, W)
    term2 = x_sq_cs[W:2 * W] - x_sq_cs[:W]

    # Term 3: cross-correlation via zero-padded FFT (linear, not circular)
    fft_size = 1
    while fft_size < N:
        fft_size <<= 1
    fft_size <<= 1

    a = np.zeros(fft_size)
    b = np.zeros(fft_size)
    a[:W] = signal[:W]
    b[:N] = signal[:N]
    xcorr = np.fft.irfft(np.conj(np.fft.rfft(a)) * np.fft.rfft(b))[:W]

    diff = term1 + term2 - 2.0 * xcorr
    diff[0] = 0.0

    # --- Cumulative mean normalized difference ---
    cmnd = np.ones(W, dtype=np.float64)
    running_sum = np.cumsum(diff[1:])
    taus = np.arange(1, W, dtype=np.float64)
    cmnd[1:] = np.where(running_sum > 0, diff[1:] * taus / running_sum, 1.0)

    # --- Absolute threshold — find first τ where cmnd < threshold ---
    tau_estimate = -1
    for tau in range(2, W):
        if cmnd[tau] < threshold:
            # Find the local minimum from here
            while tau + 1 < W and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            tau_estimate = tau
            break

    if tau_estimate < 0:
        return 0.0, 0.0

    # --- Parabolic interpolation for sub-sample accuracy ---
    if 0 < tau_estimate < W - 1:
        alpha = cmnd[tau_estimate - 1]
        beta = cmnd[tau_estimate]
        gamma = cmnd[tau_estimate + 1]
        denom = 2.0 * (2.0 * beta - gamma - alpha)
        if denom != 0:
            better_tau = tau_estimate + (alpha - gamma) / denom
        else:
            better_tau = float(tau_estimate)
    else:
        better_tau = float(tau_estimate)

    # Confidence: 1 - cmnd value at the detected tau (lower cmnd = higher confidence)
    confidence = 1.0 - cmnd[tau_estimate]
    confidence = max(0.0, min(1.0, confidence))

    frequency = sample_rate / better_tau
    return frequency, confidence


class TunerAudio:
    """JACK-based audio input with YIN pitch detection running in a background thread.

    Attributes read by the UI (thread-safe via GIL for simple reads):
        note_name   (str or None)  — e.g. "A", "E", "G#"
        octave      (int or None)  — e.g. 4
        cents       (float)        — -50.0 to +50.0
        frequency   (float)        — detected frequency in Hz
        confidence  (float)        — 0.0 to 1.0
    """

    # Median filter window — odd number so median is always one of the samples
    MEDIAN_WINDOW = 7
    # Cents within this range snap to zero (reduces visual jitter near in-tune)
    CENTS_DEAD_ZONE = 1.0

    def __init__(self, buffer_size=8192):
        self.buffer_size = buffer_size
        self.sample_rate = 48000  # updated from JACK server in start()

        # Public pitch state (read by LCD update loop)
        self.note_name = None
        self.octave = None
        self.cents = 0.0
        self.frequency = 0.0
        self.confidence = 0.0

        # Smoothing state
        self._prev_cents = 0.0
        self._prev_note = None
        self._freq_history = []

        # Internal
        self._client = None
        self._running = False
        self._thread = None
        # Lock-free buffer: JACK callback writes, detection thread reads.
        # The callback fills _ring up to buffer_size then signals _data_ready.
        # While the detection thread processes, the callback drops incoming audio
        # (acceptable for a tuner — we just skip one analysis window).
        self._ring = np.zeros(buffer_size, dtype=np.float32)
        self._write_pos = 0
        self._data_ready = threading.Event()

    def start(self):
        """Create JACK client, connect to system input, start detection thread."""
        if not JACK_AVAILABLE:
            logging.error("Cannot start tuner: JACK-Client not installed")
            return False

        # The JACK server runs as the 'jack' user; the pi-stomp service runs as root.
        # JACK_PROMISCUOUS_SERVER allows cross-user client connections.
        os.environ.setdefault("JACK_PROMISCUOUS_SERVER", "jack")

        try:
            self._client = jack.Client("pistomp-tuner", no_start_server=True)
        except jack.JackError as e:
            logging.error("Cannot create JACK client for tuner: %s" % e)
            return False

        # Use the actual JACK server sample rate for correct pitch detection
        self.sample_rate = self._client.samplerate
        logging.info("Tuner: JACK sample rate = %d, block size = %d"
                     % (self._client.samplerate, self._client.blocksize))

        self._input_port = self._client.inports.register("input")

        # Lock-free process callback — no mutexes, no allocations.
        # Fills _ring linearly; once full, signals _data_ready and stops
        # writing until the detection thread resets _write_pos.
        @self._client.set_process_callback
        def process(frames):
            data = self._input_port.get_array()
            n = len(data)
            pos = self._write_pos
            remaining = self.buffer_size - pos
            if remaining <= 0:
                return  # buffer full, waiting for detection thread
            to_copy = min(n, remaining)
            self._ring[pos:pos + to_copy] = data[:to_copy]
            self._write_pos = pos + to_copy
            if self._write_pos >= self.buffer_size:
                self._data_ready.set()

        try:
            self._client.activate()
        except jack.JackError as e:
            logging.error("Cannot activate JACK tuner client: %s" % e)
            self._client.close()
            self._client = None
            return False

        # Connect to system capture ports (guitar input)
        connected = False
        try:
            capture_ports = self._client.get_ports(is_physical=True, is_output=True)
            if capture_ports:
                logging.info("Tuner: capture ports available: %s"
                             % [p.name for p in capture_ports])
                self._client.connect(capture_ports[0], self._input_port)
                logging.info("Tuner: connected to %s" % capture_ports[0].name)
                connected = True
            else:
                logging.warning("Tuner: no physical capture ports found")
        except jack.JackError as e:
            logging.warning("Tuner: could not connect to capture port: %s" % e)

        if not connected:
            logging.error("Tuner: no audio input connected — tuner will not detect pitch")

        self._running = True
        self._thread = threading.Thread(target=self._detection_loop, daemon=True)
        self._thread.start()

        logging.info("Tuner audio started (connected=%s)" % connected)
        return True

    def stop(self):
        """Stop detection thread and close JACK client."""
        self._running = False
        self._data_ready.set()  # unblock the thread if waiting

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._client is not None:
            try:
                self._client.deactivate()
                self._client.close()
            except Exception as e:
                logging.warning("Tuner: error closing JACK client: %s" % e)
            self._client = None

        # Reset state
        self.note_name = None
        self.octave = None
        self.cents = 0.0
        self.frequency = 0.0
        self.confidence = 0.0
        self._freq_history.clear()

        logging.info("Tuner audio stopped")

    def _detection_loop(self):
        """Background thread: read buffer, run YIN, update pitch attributes."""
        while self._running:
            # Wait until JACK callback has filled the buffer
            self._data_ready.wait(timeout=0.5)
            if not self._running:
                break
            self._data_ready.clear()

            if self._write_pos < self.buffer_size:
                continue

            # Copy the buffer, then allow the callback to start writing again
            buf = self._ring.copy()
            self._write_pos = 0

            # Check signal level — ignore silence
            rms = np.sqrt(np.mean(buf * buf))
            if rms < 0.005:
                self.note_name = None
                self.octave = None
                self.cents = 0.0
                self.frequency = 0.0
                self.confidence = 0.0
                self._freq_history.clear()
                self._prev_note = None
                continue

            # Run YIN pitch detection
            freq, conf = _yin_pitch(buf.astype(np.float64), self.sample_rate)

            if freq > 0 and conf > 0.5:
                # Median filter: collect recent frequencies, use median to
                # reject outliers (e.g. occasional octave errors or noise spikes)
                self._freq_history.append(freq)
                if len(self._freq_history) > self.MEDIAN_WINDOW:
                    self._freq_history.pop(0)

                if len(self._freq_history) >= 3:
                    median_freq = float(np.median(self._freq_history))
                else:
                    median_freq = freq

                note, octave, cents = _freq_to_note(median_freq)

                # Smooth cents using confidence-weighted EMA when note is stable
                if note == self._prev_note:
                    alpha = 0.1 + 0.3 * conf * conf
                    cents = alpha * cents + (1.0 - alpha) * self._prev_cents

                # Dead zone: snap near-zero cents to exactly zero
                if abs(cents) < self.CENTS_DEAD_ZONE:
                    cents = 0.0

                self._prev_cents = cents
                self._prev_note = note

                self.note_name = note
                self.octave = octave
                self.cents = cents
                self.frequency = median_freq
                self.confidence = conf
            else:
                self.note_name = None
                self.octave = None
                self.cents = 0.0
                self.frequency = 0.0
                self.confidence = 0.0
                self._freq_history.clear()
                self._prev_note = None
