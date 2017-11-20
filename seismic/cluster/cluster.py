"""
Clustering of events and station for 3d inversion input files.
"""
from __future__ import print_function, absolute_import
import os
import click
import logging
import csv
import pandas as pd
from obspy import read_events
from obspy.geodetics import locations2degrees
from seismic import pslog
from inventory.parse_inventory import gather_isc_stations, Station


log = logging.getLogger(__name__)

column_names = ['source_block', 'station_block',
                'residual', 'event_number',
                'source_longitude', 'source_latitude',
                'source_depth', 'station_longitude', 'station_latitude',
                'observed_tt', 'locations2degrees', 'P_or_S']

station_metadata = os.path.join('inventory', 'stations.csv')


@click.group()
@click.option('-v', '--verbosity',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR']),
              default='INFO', help='Level of logging')
def cli(verbosity):
    pslog.configure(verbosity)


@cli.command()
@click.argument('events_dir',
                type=click.Path(exists=True, file_okay=False, dir_okay=True,
                                writable=False, readable=True,
                                resolve_path=True))
@click.option('-o', '--output_file',
              type=str, default='outfile',
              help='output arrivals file basename')
@click.option('-x', '--nx', type=int, default=1440,
              help='number of segments from 0 to 360 degrees for longitude')
@click.option('-y', '--ny', type=int, default=720,
              help='number of segments from 0 to 180 degrees for latitude')
@click.option('-z', '--dz', type=float, default=25.0,
              help='unit segment length of depth in meters')
@click.option('-w', '--wave_type',
              type=click.Choice(['P S', 'Pn Sn', 'Pg Sg', 'p s']),
              default='P S',
              help='Wave type pair to generate inversion inputs')
def gather(events_dir, output_file, nx, ny, dz,
           wave_type):
    """
    Gather all source-station block pairs for all events in a directory.
    """
    log.info("Gathering all arrivals")
    events = read_events(os.path.join(events_dir, '*.xml')).events

    stations = read_stations(station_metadata)
    isc_stations = gather_isc_stations()
    stations.update(isc_stations)
    process_many_events(events, nx, ny, dz, output_file, stations, wave_type)
    log.info('Gathered all arrivals and saved csv files')


def process_many_events(events, nx, ny, dz, output_file, stations, wave_type):
    # Wave type pair to dump arrivals lists for
    p_type, s_type = wave_type.split()
    p_handle = open(output_file + '_' + p_type + '.csv', 'w')
    s_handle = open(output_file + '_' + s_type + '.csv', 'w')
    p_writer = csv.writer(p_handle)
    s_writer = csv.writer(s_handle)
    for e in events:
        process_event(e, stations, p_writer, s_writer, nx, ny, dz, wave_type)
        log.debug('processed event {}'.format(e.resource_id))
    p_handle.close()
    s_handle.close()


@cli.command()
@click.argument('output_file',
                type=click.File(mode='r'))
@click.option('-s', '--sorted_file',
              type=click.File(mode='w'), default='sorted.csv',
              help='output sorted and filter file.')
def sort(output_file, sorted_file):
    """
    Sort based on the source and station block number.
    Filter based on median of observed travel time.

    If there are multiple source and station block combinations, we keep the
    row corresponding to the median observered travel time (observed_tt).

    :param output_file: output file from the gather stage
    :return: None

    """

    cluster_data = pd.read_csv(output_file, header=None,
                               names=column_names)
    cluster_data.sort_values(by=['source_block', 'station_block'],
                             inplace=True)
    groups = cluster_data.groupby(by=['source_block', 'station_block'])
    keep = []
    for _, group in groups:
        med = group['observed_tt'].median()
        # if median is not a unique match keep only one row
        keep.append(group[group['observed_tt'] == med][:1])

    final_df = pd.concat(keep)
    final_df.to_csv(sorted_file, header=True)


@cli.command()
@click.argument('p_file', type=click.File(mode='r'))
@click.argument('s_file', type=click.File(mode='r'))
@click.option('-p', '--matched_p_file',
              type=click.File(mode='w'), default='matched_p.csv',
              help='output matched p file.')
@click.option('-s', '--matched_s_file',
              type=click.File(mode='w'), default='matched_s.csv',
              help='output matched s file.')
def match(p_file, s_file, matched_p_file, matched_s_file):
    """
    Match source and station blocks and output files with matched source and
    station blocks.

    :param p_file: str
        path to sorted P arrivals
    :param s_file: str
        path to sorted S arrivals
    :param matched_p_file: str, optional
        output p arrivals file with matched p and s arrivals source-block
        combinations
    :param matched_s_file: str, optional
        output s arrivals file with matched p and s arrivals source-block
        combinations

    :return:None
    """
    p_arr = pd.read_csv(p_file)
    s_arr = pd.read_csv(s_file)

    blocks = pd.merge(p_arr[['source_block', 'station_block']],
                      s_arr[['source_block', 'station_block']],
                      how='inner',
                      on=['source_block', 'station_block'])
    matched_P = pd.merge(p_arr, blocks, how='inner',
                         on=['source_block', 'station_block'])[column_names]
    matched_S = pd.merge(s_arr, blocks, how='inner',
                         on=['source_block', 'station_block'])[column_names]
    matched_P.to_csv(matched_p_file, index=False, header=False)
    matched_S.to_csv(matched_s_file, index=False, header=False)


def process_event(event, stations, p_writer, s_writer, nx, ny, dz, wave_type):
    """
    :param event: obspy.core.event class instance
    :param stations: dict
        stations dict
    :param p_writer: p_file handle
    :param s_writer: s_file handle
    :param nx: int
        number of segments from 0 to 360 degrees for longitude
    :param ny: int
        number of segments from 0 to 180 degrees for latitude
    :param dz: float
        unit segment length of depth in meters
    :param wave_type: str
        Wave type pair to generate inversion inputs. See `gather` function.
    :return: None

    """
    p_type, s_type = wave_type.split()

    # use timestamp as the event number
    ev_number = int(event.creation_info.creation_time.timestamp * 1e6)
    origin = event.preferred_origin()

    # other event parameters we need
    ev_latitude = origin.latitude
    ev_longitude = origin.longitude
    ev_depth = origin.depth

    dx = 360. / nx
    dy = 180. / ny
    event_block = _find_block(dx, dy, dz, nx, ny,
                              origin.latitude,
                              origin.longitude,
                              z=origin.depth)
    for arr in origin.arrivals:
        sta_code = arr.pick_id.get_referred_object(
        ).waveform_id.station_code

        # ignore arrivals not in stations dict, workaround for now for
        # ENGDAHL/ISC events
        # TODO: remove this condition once all ISC/ENGDAHL stations are
        # available
        # Actually it does not hurt retaining this if condition. In case,
        # a station comes in which is not in the dict, the data prep will
        # still work
        # Note some stations are still missing even after taking into account
        #  of all seiscomp3 stations, ISC and ENGDAHL stations
        if sta_code not in stations:
            log.warning('Station {} not found in inventory'.format(sta_code))
            continue
        sta = stations[sta_code]

        degrees_to_source = locations2degrees(ev_latitude, ev_longitude,
                                              float(sta.latitude),
                                              float(sta.longitude))

        # ignore stations more than 90 degrees from source
        if degrees_to_source > 90.0:
            log.info('Ignored this station arrival as distance from source '
                     'is {} degrees'.format(degrees_to_source))
            continue

        # TODO: use station.elevation information
        station_block = _find_block(dx, dy, dz, nx, ny,
                                    float(sta.latitude), float(sta.longitude),
                                    z=0.0)

        # phase_type == 1 if P and 2 if S
        if arr.phase in wave_type.split():
            writer = p_writer if arr.phase == p_type else s_writer
            writer.writerow([
                event_block, station_block, arr.time_residual,
                ev_number, ev_longitude, ev_latitude, ev_depth,
                sta.longitude, sta.latitude,
                (arr.pick_id.get_referred_object().time.timestamp -
                 origin.time.timestamp), degrees_to_source,
                1 if arr.phase == p_type else 2
                ])
        else:  # ignore the other phases
            pass


def _find_block(dx, dy, dz, nx, ny, lat, lon, z):
    y = 90. - lat
    x = lon if lon > 0 else lon + 360.0
    i = round(x / dx) + 1
    j = round(y / dy) + 1
    k = round(z / dz) + 1
    block_number = (k - 1) * nx * ny + (j - 1) * nx + i
    return int(block_number)


def read_stations(station_file):
    """
    Read station location from a csv file.
    :param station_file: str
        csv stations file handle passed in by click
    :return: stations_dict: dict
        dict of stations indexed by station_code for quick lookup
    """
    log.info('Reading seiscomp3 exported stations file')
    stations_dict = {}
    with open(station_file, 'r') as csv_file:
        reader = csv.reader(csv_file)
        next(reader)  # skip header
        for station in map(Station._make, reader):
            stations_dict[station.station_code] = station
        log.info('Done reading seiscomp3 station files')
        return stations_dict