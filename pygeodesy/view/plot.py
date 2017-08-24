#-*- coding: utf-8 -*_

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import sys
import os

from ..db.Engine import Engine
import pygeodesy.instrument as instrument

# Define the default options
defaults = {
    'input': 'sqlite:///data.db',
    'component': None,
    'residual': False,
    'stations': None,
    'statlist': None,
    'save': False,
    'tstart': None,
    'tend': None,
    'ylim': (None, None),
    'model': 'filt',
    'output_dir': 'figures',
    'figwidth': 10,
    'figheight': 6,
    'kml': None,
    'detrend_data': True,
}


def plot(optdict):

    # Update the options
    opts = defaults.copy()
    opts.update(optdict)

    # Map matplotlib color coes to the seaborn palette
    try:
        import seaborn as sns
        sns.set_color_codes()
    except ImportError:
        pass

    # Create engine for input database
    engine = Engine(url=opts['input'])

    # Initialize an instrument
    inst = instrument.select(opts['type'])

    # Plot KML if requested and exit
    if opts['kml'] is not None:
        from .kml import make_kml
        make_kml(engine, opts['kml'])
        return

    # Get the list of stations to plot
    if opts['stations'] == 'all':
        import pygeodesy.network.Network as Network
        network = Network(inst, engine)
        statnames = network.names
    else: 
        statnames = opts['stations'].split()

    # Read data after checking for existence of component
    if opts['component'] == 'all':
        components = engine.components()
    elif opts['component'] not in engine.components():
        components = [engine.components()[0]]
    else:
        components = [opts['component']]

    # Read data array
    dates = engine.dates()

    # Determine plotting bounds
    tstart = np.datetime64(opts['tstart']) if opts['tstart'] is not None else None
    tend = np.datetime64(opts['tend']) if opts['tend'] is not None else None

    # Determine y-axis bounds
    if type(opts['ylim']) is str:
        y0, y1 = [float(y) for y in opts['ylim'].split(',')]
    else:
        y0, y1 = opts['ylim']

    # Set the figure size
    figsize = (int(opts['figwidth']), int(opts['figheight'])) 
    fig, axes = plt.subplots(nrows=len(components), figsize=figsize)
    if type(axes) not in (list, np.ndarray):
        axes = [axes]

    # Check output directory exists if we're saving
    if opts['save'] and not os.path.isdir(opts['output_dir']):
        os.makedirs(opts['output_dir'])

    # Loop over stations
    for statname in statnames:

        # Check if we had previously closed the figure and need to make new one
        if fig is None and axes is None:
            fig, axes = plt.subplots(nrows=len(components), figsize=figsize)
            if type(axes) not in (list, np.ndarray):
                axes = [axes]

        print(statname)

        for ax, component in zip(axes, components):

            # Read data
            data = pd.read_sql_table(component, engine.engine, columns=[statname,])
            data = data[statname].values.squeeze()
            
            # Try to read model data
            fit = model_and_detrend(data, engine, statname, component,
                opts['model'], detrend_data=opts['detrend_data'])

            # Remove means
            dat_mean = np.nanmean(data)
            data -= dat_mean
            fit -= dat_mean
            residual = data - fit

            # Plot residuals
            if opts['residual']:
                std = np.nanmedian(np.abs(residual - np.nanstd(residual)))
                residual[np.abs(residual) > 4*std] = np.nan
                line, = ax.plot(dates, residual, 'o', alpha=0.6, zorder=10)
            # Or data and model
            else:
                line, = ax.plot(dates, data, 'o', alpha=0.6, zorder=10)
                ax.plot(dates, fit, '-r', linewidth=6, zorder=11)

                # Also try to read "raw" data (for CME results)
                try:
                    raw = pd.read_sql_table('raw_' + component, 
                        engine.engine, columns=[statname,])
                    raw = raw.values.squeeze() - dat_mean
                    ax.plot(dates, raw, 'sg', alpha=0.7, zorder=9)
                except:
                    pass

            ax.tick_params(labelsize=18)
            ax.set_ylabel(component, fontsize=18)
            ax.set_xlim(tstart, tend)
            ax.set_ylim(y0, y1)
            #ax.set_xticks(ax.get_xticks()[::2])

        axes[0].set_title(statname, fontsize=18)
        axes[-1].set_xlabel('Year', fontsize=18)

        #plt.savefig('temp.png'); sys.exit()
        #plt.show()
        #sys.exit()

        if opts['save']:
            plt.savefig('%s/%s_%s.png' % (opts['output_dir'], statname, component), 
                dpi=200, bbox_inches='tight')
            fig = axes = None
        else:
            plt.show()
        plt.close('all') 


def model_and_detrend(data, engine, statname, component, model, detrend_data=True):

    # Get list of tables in the database
    tables = engine.tables(asarray=True)

    # Keys to look for
    model_comp = '%s_%s' % (model, component) if model != 'filt' else 'None'
    filt_comp = 'filt_' + component

    # Construct list of model components to remove (if applicable)
    if model == 'secular':
        parts_to_remove = ['seasonal', 'transient', 'step']
    elif model == 'seasonal':
        parts_to_remove = ['secular', 'transient', 'step']
    elif model == 'transient':
        parts_to_remove = ['secular', 'seasonal', 'step']
    elif model == 'step':
        parts_to_remove = ['secular', 'seasonal', 'transient']
    elif model in ['full', 'filt']:
        parts_to_remove = []
    else:
        assert False, 'Unsupported model component %s' % model

    # Make the model and detrend the data
    fit = np.nan * np.ones_like(data)
    if model_comp in tables:

        # Read full model data
        fit = pd.read_sql_table('full_' + component, engine.engine, 
            columns=[statname,]).values.squeeze()

        # Remove parts we do not want
        for ftype in parts_to_remove:
            try:
                signal = pd.read_sql_table('%s_%s' % (ftype, component), engine.engine,
                    columns=[statname,]).values.squeeze()
                fit -= signal
                if detrend_data:
                    data -= signal
                print('removed', ftype)
            except ValueError:
                pass

    elif filt_comp in tables and model_comp not in tables:
        fit = pd.read_sql_table('filt_%s' % component, engine.engine,
            columns=[statname,]).values.squeeze()

    return fit


# end of file
