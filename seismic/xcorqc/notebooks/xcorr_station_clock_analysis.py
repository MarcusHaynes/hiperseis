#!/usr/bin/env python
# coding: utf-8
"""
Functions for computing estimated GPS clock corrections based on station pair cross-correlation
and plotting in standard layout.

Highest level plotting function is ``plot_xcorr_file_clock_analysis``.
"""

import os
from textwrap import wrap
import datetime

import numpy as np
import scipy
from scipy import signal
import matplotlib.dates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from dateutil import rrule

import obspy
from netCDF4 import Dataset as NCDataset
from seismic.ASDFdatabase import FederatedASDFDataSet

from analytic_plot_utils import distance


# Get station codes from file name
def station_codes(filename):
    """
    Convert a netCDF4 .nc filename generated by correlator to the corresponding
    station codes in the format ``NETWORK.STATION``

    Assumed format: ``NET1.STAT1.NET2.STA2.nc``

    :param filename: The ``.nc`` filename from which to extract the station and network codes
    :type filename: str
    :return: Station code for each station (code1, code2)
    :rtype: tuple(str, str)
    """
    _, fname = os.path.split(filename)
    parts = fname.split('.')
    sta1 = '.'.join(parts[0:2])
    sta2 = '.'.join(parts[2:4])
    return (sta1, sta2)


def station_coords(federated_ds, code, timestamp):
    """
    Get station location coordinates of z-channel for a given station code.
    In practice a time period is required to query station metadata, so an
    end time of 30 days after the query timestamp is used.

    Preferentially picks BHZ channel if available, otherwise the first available
    z-channel.

    :param federated_ds: Federated dataset to query for station coordinates
    :type federated_ds: seismic.ASDFdatabase.FederatedASDFDataSet
    :param code: Station and network code in the format ``NETWORK.STATION``
    :type code: str
    :param timestamp: UTC datetime string at which to enquire about the location.
    :type timestamp: str
    :return: Pair of station coordinates in format [latitude, longitude]
    :rtype: list
    """
    query_window_seconds = 3600 * 24 * 30
    net, sta = code.split('.')
    # Perform query on Federated Dataset
    sta_records = federated_ds.get_stations(timestamp, obspy.UTCDateTime(timestamp) +
                                            query_window_seconds, network=net, station=sta)
    # Filter query results to *HZ channels
    z_records = [r for r in sta_records if r[3][1:3] == 'HZ']
    if len(z_records) != 1:
        print("WARNING: More than one matching channel record found for station {} coords:\n{}"
              .format(code, z_records))
        PREFERRED_CHANNEL = 'BHZ'
        z_record = None
        for rec in z_records:
            if rec[3] == PREFERRED_CHANNEL:
                z_record = rec
                break
        if z_record is None:
            z_record = z_records[0]
        print("WARNING: Automatically chose channel {}".format(z_record[3]))
    else:
        z_record = z_records[0]
    return z_record[4:6]


def station_distance(federated_ds, code1, code2, timestamp):
    """
    Determine the distance in km between a pair of stations at a given time.

    :param federated_ds: Federated dataset to query for station coordinates
    :type federated_ds: seismic.ASDFdatabase.FederatedASDFDataSet
    :param code1: Station and network code in the format ``NETWORK.STATION`` for first station
    :type code1: str
    :param code2: Station and network code in the format ``NETWORK.STATION`` for second station
    :type code2: str
    :param timestamp: UTC datetime string at which to enquire about the distance between stations.
    :type timestamp: str
    :return: Distance between stations in kilometers
    :rtype: float
    """

    coords1 = station_coords(federated_ds, code1, timestamp)
    coords2 = station_coords(federated_ds, code2, timestamp)
    return distance(coords1, coords2)


def compute_estimated_clock_corrections(rcf, snr_mask, ccf_masked, x_lag, pcf_cutoff_threshold):
    """
    Compute the estimated GPS clock corrections given series of cross-correlation functions
    and an overall reference correlation function (rcf, the mean of valid samples of the
    cross-correlation time series).
    
    :param rcf: Pre-computed reference correlation function (RCF) as per Hable et. al (2018)
    :type rcf: np.array
    :param snr_mask: Mask of which samples meet minimum SNR criteria
    :type snr_mask: numpy array mask
    :param ccf_masked: Time series of cross-correlation functions, masked by
                       externally decided validity flags.
    :type ccf_masked: numpy.ma.masked_array
    :param x_lag: The x-axis, the lag
    :type x_lag: numpy.array
    :param pcf_cutoff_threshold: Minimum threshold for Pearson correlation factor.
    :type pcf_cutoff_threshold: float
    :return: Corrected RCF after first order corrections; estimated clock corrections;
             final per-sample cross-correlation between corrected RCF and each sample
    :rtype: (numpy.array [1D], numpy.array [1D], numpy.array [2D])
    """
    # Make an initial estimate of the shift, and only mask out a row if the Pearson coefficient
    # is less than the threshold AFTER applying the shift. Otherwise we will be masking out
    # some of the most interesting regions where shifts occur.
    correction = []
    ccf_shifted = []
    for row in ccf_masked:
        if np.ma.is_masked(row) or rcf is None:
            correction.append(np.nan)
            ccf_shifted.append(np.array([np.nan] * ccf_masked.shape[1]))
            continue

        # The logic here needs to be expanded to allow for possible mirroring of the CCF
        # as well as a shift.
        c3 = scipy.signal.correlate(rcf, row, mode='same')
        c3 /= np.max(c3)
        peak_index = np.argmax(c3)
        shift_size = int(peak_index - len(c3) / 2)
        row_shifted = np.roll(row, shift_size)
        # Zero the rolled in values
        if shift_size > 0:
            row_shifted[0:shift_size] = 0
        elif shift_size < 0:
            row_shifted[shift_size:] = 0
        ccf_shifted.append(row_shifted)
        # Store first order corrections.
        peak_lag = x_lag[peak_index]
        correction.append(peak_lag)
    # end for
    correction = np.array(correction)
    ccf_shifted = np.array(ccf_shifted)
    # Recompute the RCF with first order clock corrections.
    rcf_corrected = np.nanmean(ccf_shifted[snr_mask, :], axis=0)

    # For the Pearson coeff threshold, apply it against the CORRECTED RCF after the application
    # of estimated clock corrections.
    row_rcf_crosscorr = []
    for i, row in enumerate(ccf_masked):
        if np.ma.is_masked(row) or rcf is None:
            row_rcf_crosscorr.append([np.nan] * ccf_masked.shape[1])
            continue

        pcf_corrected, _ = scipy.stats.pearsonr(rcf_corrected, ccf_shifted[i, :])
        if pcf_corrected < pcf_cutoff_threshold:
            correction[i] = np.nan
            row_rcf_crosscorr.append([np.nan] * ccf_masked.shape[1])
            continue
        # Compute second order corrections based on first order corrected RCF
        c3 = scipy.signal.correlate(rcf_corrected, row, mode='same')
        c3 /= np.max(c3)
        peak_index = np.argmax(c3)
        peak_lag = x_lag[peak_index]
        correction[i] = peak_lag
        row_rcf_crosscorr.append(c3)
    # end for
    row_rcf_crosscorr = np.array(row_rcf_crosscorr)

    return rcf_corrected, correction, row_rcf_crosscorr


def plot_xcorr_time_series(ax, x_lag, y_times, xcorr_data, use_formatter=False):

    np_times = np.array([datetime.datetime.utcfromtimestamp(v) for v in y_times]).astype('datetime64[s]')
    gx, gy = np.meshgrid(x_lag, np_times)
    im = ax.pcolormesh(gx, gy, xcorr_data, cmap='RdYlBu_r', vmin=0, vmax=1, rasterized=True)

    if use_formatter:
        date_formatter = matplotlib.dates.DateFormatter("%Y-%m-%d")
        date_locator = matplotlib.dates.WeekdayLocator(byweekday=rrule.SU)
        ax.yaxis.set_major_formatter(date_formatter)
        ax.yaxis.set_major_locator(date_locator)
    else:
        labels = np.datetime_as_string(np_times, unit='D')
        ax.set_yticks(np_times[::7])
        ax.set_yticklabels(labels[::7])

    ax.set_xlabel('Lag [s]')
    ax.set_ylabel('Days')

    ax_pos = ax.get_position()
    cax = plt.axes([ax_pos.x0 + 0.025, ax_pos.y1 - 0.1, 0.015, 0.08])

    plt.colorbar(im, cax=cax, orientation='vertical', ticks=[0, 1])


def plot_reference_correlation_function(ax, x_lag, rcf, rcf_corrected, snr_threshold):
    if rcf is not None:
        ax.axvline(x_lag[np.argmax(rcf)], c='#c66da9', lw=2,
                   label='{:5.2f} s'.format(x_lag[np.argmax(rcf)]))
        ax.plot(x_lag, rcf, c='#42b3f4',
                label="Reference CCF (RCF)\n"
                      "Based on Subset\n"
                      "with SNR > {}".format(snr_threshold))
        ax.plot(x_lag, rcf_corrected, '--', c='#00b75b', alpha=0.8, label="First order\ncorrected RCF")
        ax.legend()
    else:
        ax.text(0.5, 0.5, 'REFERENCE CCF:\nINSUFFICIENT SNR', horizontalalignment='center',
                verticalalignment='center', transform=ax.transAxes, fontsize=16)

    ax.set_xticklabels([])


def plot_stacked_window_count(ax, x_nsw, y_times):
    ax.plot(x_nsw, y_times, c='#529664')
    ax.set_ylim((min(y_times), max(y_times)))
    ax.set_yticklabels([])
    ax.set_xlabel('\n'.join(wrap('# of Hourly Stacked Windows', 12)))
    xtl = ax.get_xticklabels()
    xtl[0].set_visible(False)
    xtl[-1].set_visible(False)


def plot_snr_histogram(ax, snr, time_window, nbins=10):
    ax.hist(snr.compressed(), fc='#42b3f4', ec='none', bins=nbins)
    ax.set_xlabel('SNR: Daily CCFs [-{:d}, {:d}]s'.format(time_window, time_window))
    ax.set_ylabel('Frequency')
    xtl = ax.get_xticklabels()
    xtl[0].set_visible(False)
    xtl[-1].set_visible(False)


def plot_pearson_corr_coeff(ax, rcf, ccf_masked, y_times):
    cc = []
    for row in ccf_masked:
        if np.ma.is_masked(row) or rcf is None:
            cc.append(np.nan)
        else:
            pcf, _ = scipy.stats.pearsonr(rcf, row)
            cc.append(pcf)
    # end for
    pcf = np.array(cc)
    # Compute CC mean
    ccav = np.mean(np.ma.masked_array(pcf, mask=np.isnan(pcf)))

    ax.plot(pcf, y_times, c='#d37f26')
    ax.set_ylim((min(y_times), max(y_times)))
    ax.set_yticklabels([])
    ax.set_xticks([0, 1])
    ax.set_xlabel('\n'.join(wrap('Raw Pearson\nCoeff. (vs RCF)', 15)))
    ax.text(0.5, 0.98, '$PC_{av}$' + '={:3.3f}'.format(ccav), horizontalalignment='center',
            verticalalignment='top', transform=ax.transAxes)
    ax.axvline(ccav, c='#d37f26', linestyle='--', lw=2, linewidth=1, alpha=0.7)


def plot_estimated_timeshift(ax, x_lag, y_times, correction, annotation=None, row_rcf_crosscorr=None):

    if row_rcf_crosscorr is not None:
        # Line plot laid over the top of RCF * CCF
        np_times = np.array([datetime.datetime.utcfromtimestamp(v) for v in y_times]).astype('datetime64[s]')
        gx, gy = np.meshgrid(x_lag, np_times)
        plot_data = row_rcf_crosscorr
        crange_floor = 0.7
        plot_data[np.isnan(plot_data)] = crange_floor
        plot_data[(plot_data < crange_floor)] = crange_floor
        ax.pcolormesh(gx, gy, plot_data, cmap='RdYlBu_r', rasterized=True)
        ax.set_ylim((min(np_times), max(np_times)))
        xrange = 1.2 * np.nanmax(np.abs(correction))
        ax.set_xlim((-xrange, xrange))
        col = '#ffffff'
        ax.plot(correction, np_times, 'o--', color=col, fillstyle='none', markersize=4, alpha=0.8)
    else:
        # Plain line plot
        ax.plot(correction, y_times, 'o-', c='#f22e62', lw=1.5, fillstyle='none', markersize=4)
        ax.set_ylim((min(y_times), max(y_times)))
        ax.grid(":", color='#80808080')

    ax.set_yticklabels([])
    xtl = ax.get_xticklabels()
    xtl[0].set_visible(False)
    xtl[-1].set_visible(False)
    ax.set_xlabel('\n'.join(wrap('Estimated Timeshift [s]: RCF * CCF', 15)))
    if annotation is not None:
        ax.text(0.05, 0.98, annotation, color='#000000', horizontalalignment='left',
                verticalalignment='top', transform=ax.transAxes, rotation=90)


def plot_xcorr_file_clock_analysis(src_file, asdf_dataset, time_window, snr_threshold,
                                   show=True, underlay_rcf_xcorr=False,
                                   pdf_file=None, png_file=None):
    # Read xcorr data
    xcdata = NCDataset(src_file, 'r')

    xc_start_times = xcdata.variables['IntervalStartTimes'][:]
    xc_end_times = xcdata.variables['IntervalEndTimes'][:]
    xc_lag = xcdata.variables['lag'][:]
    xc_xcorr = xcdata.variables['xcorr'][:, :]
    xc_num_stacked_windows = xcdata.variables['NumStackedWindows'][:]
    xcdata.close()
    xcdata = None

    start_utc_time = obspy.UTCDateTime(xc_start_times[0])
    end_utc_time = obspy.UTCDateTime(xc_end_times[-1])

    start_time = str(start_utc_time)
    end_time = str(end_utc_time)
    print("Date range for file {}:\n    {} -- {}".format(src_file, start_time, end_time))

    origin_code, dest_code = station_codes(src_file)
    dist = station_distance(asdf_dataset, origin_code, dest_code, start_time)

    # Extract primary data
    lag_indices = np.squeeze(np.argwhere(np.fabs(np.round(xc_lag, decimals=2)) == time_window))
    start_times = xc_start_times
    lag = xc_lag[lag_indices[0]:lag_indices[1]]
    ccf = xc_xcorr[:, lag_indices[0]:lag_indices[1]]
    nsw = xc_num_stacked_windows

    # Compute derived quantities used by multiple axes
    zero_row_mask = (np.all(ccf == 0, axis=1))
    valid_mask = np.ones_like(ccf)
    valid_mask[zero_row_mask, :] = 0
    valid_mask = (valid_mask > 0)
    ccf_masked = np.ma.masked_array(ccf, mask=~valid_mask)
    snr = np.nanmax(ccf_masked, axis=1) / np.nanstd(ccf_masked, axis=1)
    if np.any(snr > snr_threshold):
        snr_mask = (snr > snr_threshold)
        rcf = np.nanmean(ccf_masked[snr_mask, :], axis=0)
    else:
        snr_mask = None
        rcf = None

    PCF_CUTOFF_THRESHOLD = 0.5
    rcf_corrected, correction, row_rcf_crosscorr = \
        compute_estimated_clock_corrections(rcf, snr_mask, ccf_masked, lag, PCF_CUTOFF_THRESHOLD)

    # -----------------------------------------------------------------------
    # Master layout and plotting code

    fig = plt.figure(figsize=(11.69, 16.53))
    fig.suptitle("Station: {}, Dist. to {}: {:3.2f} km".format(origin_code, dest_code, dist), fontsize=16, y=1)

    ax1 = fig.add_axes([0.1, 0.075, 0.5, 0.725])

    label_pad = 0.05
    ax2 = fig.add_axes([0.1, 0.8, 0.5, 0.175])  # reference CCF (accumulation of daily CCFs)
    ax3 = fig.add_axes([0.6, 0.075, 0.1, 0.725])  # number of stacked windows
    ax4 = fig.add_axes([0.6 + label_pad, 0.8 + 0.6 * label_pad, 0.345, 0.175 - 0.6 * label_pad])  # SNR histogram
    ax5 = fig.add_axes([0.7, 0.075, 0.1, 0.725])  # Pearson coeff
    ax6 = fig.add_axes([0.8, 0.075, 0.195, 0.725])  # estimate timeshifts

    # Plot CCF image =======================
    plot_xcorr_time_series(ax1, lag, start_times, ccf)

    # Plot CCF-template (reference CCF) ===========
    plot_reference_correlation_function(ax2, lag, rcf, rcf_corrected, snr_threshold)

    # Plot number of stacked windows ==============
    plot_stacked_window_count(ax3, nsw, start_times)

    # Plot histogram
    plot_snr_histogram(ax4, snr, time_window)

    # Plot Pearson correlation coefficient=========
    plot_pearson_corr_coeff(ax5, rcf, ccf_masked, start_times)

    # plot Timeshift =====================
    annotation = 'Min. corrected Pearson Coeff={:3.3f}'.format(PCF_CUTOFF_THRESHOLD)
    if underlay_rcf_xcorr:
        plot_estimated_timeshift(ax6, lag, start_times, correction, annotation=annotation,
                                 row_rcf_crosscorr=row_rcf_crosscorr)
    else:
        plot_estimated_timeshift(ax6, lag, start_times, correction, annotation=annotation)

    # Print and display
    if pdf_file is not None:
        pdf_out = PdfPages(pdf_file)
        pdf_out.savefig(plt.gcf(), dpi=600)
        pdf_out.close()

    if png_file is not None:
        plt.savefig(png_file, dpi=150)

    if show:
        plt.show()

    plt.close()