#!/usr/bin/env python3

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy_utils import database_exists, drop_database, create_database
import pandas as pd
import argparse
import sys
import os


def parse():
    parser = argparse.ArgumentParser(description="""
        Extract sub-network from database.""")
    parser.add_argument('database_in', type=str, 
        help='Input database.')
    parser.add_argument('database_out', type=str,
        help='Output database.')
    parser.add_argument('-list', type=str, action='store', default=None, 
        help='Input file list containing station names to extract. Default: None.')
    parser.add_argument('-poly', type=str, action='store', default=None,
        help='Input polynomial to extract region of interest. Default: None.')
    return parser.parse_args()


def main(inputs):

    # Create engine for input database
    engine_in = create_engine('sqlite:///%s' % inputs.database_in)

    # Clear output database and create it's engine
    if os.path.isfile(inputs.database_out):
        os.remove(inputs.database_out)
    engine_out = create_engine('sqlite:///%s' % inputs.database_out)
    create_database(engine_out.url)

    # Read metadata from input table
    meta = pd.read_sql_query("SELECT id, lon, lat FROM metadata;", engine_in)
    names = meta['id'].values

    # Use polynomial for a mask
    if inputs.poly:

        # Cache the raw lon/lat values
        lon, lat = meta['lon'].values, meta['lat'].values

        # Load points from polynomial file
        from matplotlib.path import Path
        plon, plat = np.loadtxt(inputs.poly, unpack=True)
    
        # Make a path object to compute mask
        poly = Path(np.column_stack((plon, plat)))
        mask = poly.contains_points(list(zip(lon, lat)))

        # Subset stations
        stations = names[mask]

    elif inputs.list:
        
        # Read stations
        input_stations = np.loadtxt(inputs.list, dtype=bytes).astype(str)

        # Keep ones that are in the database
        stations = np.intersect1d(input_stations, names)

    # Subset metadata and write to table
    meta_sub = meta[np.in1d(names, stations)].reset_index(drop=True)
    meta_sub.to_sql('metadata', engine_out)

    # Loop over components 
    for component in ('east', 'north', 'up'):

        print('Working on %s component' % component)

        # Loop over subset of stations
        data_df = None
        sigma_df = None
        for i, statname in enumerate(stations):

            # Query database for current station+component 
            data = pd.read_sql_query("SELECT DATE, %s, sigma_%s FROM tseries WHERE id = '%s';"
                % (component, component, statname), engine_in)
            new_data_df = data[['DATE', component]]
            new_sigma_df = data[['DATE', 'sigma_' + component]]

            # Use station name in columns as a unique identifier
            new_data_df.columns = ['DATE', statname]
            new_sigma_df.columns = ['DATE', statname]

            # Merge results
            if data_df is None:
                data_df, sigma_df = new_data_df, new_sigma_df
            else:
                data_df = pd.merge(data_df, new_data_df, how='outer', on='DATE')
                sigma_df = pd.merge(sigma_df, new_sigma_df, how='outer', on='DATE')

        # Set the DATE column to be the index in order to resample
        data_df.index = pd.to_datetime(data_df['DATE'], format='%Y-%m-%d %H:%M:%S.%f')
        sigma_df.index = pd.to_datetime(sigma_df['DATE'], format='%Y-%m-%d %H:%M:%S.%f')

        # Resample to an evenly spaced date range
        data_df = data_df.resample('D').sum().reset_index()
        sigma_df = sigma_df.resample('D').sum().reset_index()

        # Make date a separate column again
        data_df.rename(columns={'index': 'DATE'}, inplace=True)
        sigma_df.rename(columns={'index': 'DATE'}, inplace=True)

        # Save to table
        data_df.reset_index(drop=True).to_sql(component, engine_out)
        sigma_df.reset_index(drop=True).to_sql('sigma_' + component, engine_out)



if __name__ == '__main__':
    inputs = parse()
    main(inputs)

# end of file
