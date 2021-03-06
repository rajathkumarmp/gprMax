# Copyright (C) 2015-2016: The University of Edinburgh
#                 Authors: Craig Warren and Antonis Giannopoulos
#
# This file is part of gprMax.
#
# gprMax is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# gprMax is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with gprMax.  If not, see <http://www.gnu.org/licenses/>.

import os, argparse
import numpy as np
import matplotlib.pyplot as plt

from gprMax.exceptions import CmdInputError
from gprMax.waveforms import Waveform


"""Plot built-in waveforms that can be used for sources."""

# Parse command line arguments
parser = argparse.ArgumentParser(description='Plot built-in waveforms that can be used for sources.', usage='cd gprMax; python -m tools.plot_builtin_wave type amp freq timewindow dt')
parser.add_argument('type', help='type of waveform, e.g. gaussian, ricker etc...')
parser.add_argument('amp', type=float, help='amplitude of waveform')
parser.add_argument('freq', type=float, help='centre frequency of waveform')
parser.add_argument('timewindow', type=float, help='time window to view waveform')
parser.add_argument('dt', type=float, help='time step to view waveform')
parser.add_argument('-fft', action='store_true', default=False, help='plot FFT')
args = parser.parse_args()

# Check waveform parameters
if args.type.lower() not in Waveform.waveformtypes:
    raise CmdInputError('The waveform must have one of the following types {}'.format(','.join(Waveform.waveformtypes)))
if args.freq <= 0:
    raise CmdInputError('The waveform requires an excitation frequency value of greater than zero')

w = Waveform()
w.type = args.type
w.amp = args.amp
w.freq = args.freq
timewindow = args.timewindow
dt = args.dt

time = np.arange(0, timewindow, dt)
waveform = np.zeros(len(time))
timeiter = np.nditer(time, flags=['c_index'])

while not timeiter.finished:
    waveform[timeiter.index] = w.calculate_value(timeiter[0], dt)
    timeiter.iternext()

if args.fft:
    # Calculate magnitude of frequency spectra of waveform
    power = 10 * np.log10(np.abs(np.fft.fft(waveform))**2)
    freqs = np.fft.fftfreq(power.size, d=dt)

    # Shift powers so that frequency with maximum power is at zero decibels
    power -= np.amax(power)

    # Set plotting range to 4 times centre frequency of waveform
    pltrange = np.where(freqs > 4 * w.freq)[0][0]
    pltrange = np.s_[0:pltrange]

    fig, (ax1, ax2) = plt.subplots(nrows=1, ncols=2, num=w.type, figsize=(20, 10), facecolor='w', edgecolor='w')
    
    # Plot waveform
    ax1.plot(time, waveform, 'r', lw=2)
    ax1.set_xlabel('Time [ns]')
    ax1.set_ylabel('Amplitude')

    # Plot frequency spectra
    markerline, stemlines, baseline = ax2.stem(freqs[pltrange]/1e9, power[pltrange], '-.')
    plt.setp(baseline, 'linewidth', 0)
    plt.setp(stemlines, 'color', 'r')
    plt.setp(markerline, 'markerfacecolor', 'r', 'markeredgecolor', 'r')
    ax2.plot(freqs[pltrange]/1e9, power[pltrange], 'r', lw=2)
    ax2.set_xlabel('Frequency [GHz]')
    ax2.set_ylabel('Power [dB]')

else:
    fig, ax1 = plt.subplots(num=w.type, figsize=(20, 10), facecolor='w', edgecolor='w')

    # Plot waveform
    ax1.plot(time, waveform, 'r', lw=2)
    ax1.set_xlabel('Time [ns]')
    ax1.set_ylabel('Amplitude')

[ax.grid() for ax in fig.axes] # Turn on grid
plt.show()

# Save a PDF of the figure
#fig.savefig(os.path.dirname(os.path.abspath(__file__)) + os.sep + w.type + '.png', dpi=150, format='png', bbox_inches='tight', pad_inches=0.1)

