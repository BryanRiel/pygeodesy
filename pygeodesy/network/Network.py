#-*- coding: utf-8 -*-

import numpy as np
from scipy.stats import nanmedian
import datetime as dtime
from mpi4py import MPI
import pandas as pd
import tsinsar as ts
import shutil
import h5py
import sys

from ..utilities import datestr2tdec


class Network:
    """
    Abstract class for all time series data objects.
    """

    def __init__(self, instrument, engine, comm=None):
        """
        Initialize the network with a SQL engine.
        """

        # Initialize the MPI parameters 
        self.comm = comm or MPI.COMM_WORLD
        self.rank = self.comm.Get_rank()
        self.size = self.comm.Get_size()

        if self.rank == 0:
            print('Initializing network')

        # Save the engine and instrument
        self.inst = instrument
        self.engine = engine 

        # Get list of tables in database
        self.table_df = pd.read_sql_query("SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name;", self.engine.engine)

        # Read metadata and save to self
        self.meta = pd.read_sql_table('metadata', self.engine.engine,
            columns=['id','lat','lon','elev'])
        self.names = self.meta['id'].values
        for key in ('lat','lon','elev'):
            setattr(self, key, self.meta[key].values.astype(float))
        self.nstat = len(self.names)

        # Save the observation dates
        dates = pd.read_sql_table(self.inst.components[0], self.engine.engine,
            columns=['DATE']).values
        dates = np.array([pd.to_datetime(date, infer_datetime_format=True)
            for date in dates])
        self.dates = [dtime.datetime.utcfromtimestamp(date.astype('O') / 1.0e9)
            for date in dates]

        # Convert dates to decimal year and save
        self.tdec = np.array([datestr2tdec(pydtime=date) for date in self.dates])

        return


    def clear(self):
        """
        Clears entries for station locations and names.
        """
        for attr in ('names', 'lat', 'lon', 'elev'):
            setattr(self, attr, [])
        self.nstat = 0
        return


    def get(self, component, statid, scale=1.0, with_date=False):
        """
        Load data for a given component and station ID. Stored as a data frame.
        """
        # Load data frame
        if type(statid) in (list, tuple, np.ndarray):
            cols = ['DATE'] + list(statid) if with_date else statid
            df = pd.read_sql_table(component, self.engine.engine, columns=cols)
        elif type(statid) is str:
            cols = ['DATE', statid] if with_date else [statid,]
            df = pd.read_sql_table(component, self.engine.engine, columns=cols)
        elif statid is None:
            cols = ['DATE'] + list(self.names) if with_date else self.names
            df = pd.read_sql_table(component, self.engine.engine, columns=cols)
        
        # Apply scale parameter
        for key in df:
            if key != 'DATE':
                df[key] *= scale

        return df


    def updateMetadata(self, statlist, engine):
        """
        Compare the station list to check if we need to re-write metadata.
        """
        meta_new, update_status = engine.updateMeta(statlist)
        if update_status:
            self.meta = meta_new
            self.names = self.meta['id'].values
            for key in ('lat','lon','elev'):
                setattr(self, key, self.meta[key].values.astype(float))
            self.nstat = len(self.names)
        return


    def partitionStations(self, npart=None):
        """
        Create equal partitions of stations in the network. Using the MPI
        size to create the partitions if npart is not provided.
        """
        N = npart or self.size
        nominal_load = self.nstat // N
        if self.rank == self.size - 1:
            procN = self.nstat - self.rank * nominal_load
        else:
            procN = nominal_load
        istart = self.rank * nominal_load
        self.sub_names = self.names[istart:istart+procN]
        return self.comm.allgather(procN)


    def zeroMeanDisplacements(self):
        """
        Remove the mean of the finite values in each component of displacement.
        """
        from scipy.stats import nanmean
        for statname, stat in self.statGen:
            for component in self.components:
                dat = getattr(self, component)
                dat -= nanmean(dat)
        return


    def getDataArrays(self, order='columns', components=None, scale=1.0):
        """
        Traverses the station dictionary to construct regular sized arrays for the
        data and weights.
        """

        # Get first station to determine the number of data points
        for statname in self.names:
            df = self.get(self.inst.components[0], statname)
            ndat = df.size
            break 

        # Get components to process
        comps = components or self.inst.components
        ncomp = len(comps)

        # Construct regular arrays
        nobs = self.nstat * ncomp
        data = np.empty((ndat, nobs))
        weights = np.empty((ndat, nobs))

        # Fill them
        j = 0
        for component in comps:
            for statname in self.names:
                dat_df = self.get(component, statname, scale=scale)
                sig_df = self.get('sigma_' + component, statname, scale=1.0/scale)
                data[:,j] = dat_df[statname].values
                weights[:,j] = 1.0 / sig_df[statname].values
                j += 1

        # Custom packaging
        return_arrs = [data, weights]
        if order == 'rows':
            return_arrs = [arr.T.copy() for arr in return_arrs]
        return return_arrs
        

    def computeNetworkWeighting(self, smooth=1.0, n_neighbor=3, L0=None):
        """
        Computes the network-dependent spatial weighting based on station/ground locations.
        """
        import topoutil as tu

        # Allocate array for storing weights
        dist_weight = np.zeros((self.nstat, self.nstat))

        # Loop over stations
        rad = np.pi / 180.0
        for i in range(self.nstat):
            ref_X = tu.llh2xyz(self.lat[i]*rad, self.lon[i]*rad, self.elev[i])
            stat_dist = np.zeros((self.nstat,))
            # Loop over other stations
            for j in range(self.nstat):
                if j == i:
                    continue
                curr_X = tu.llh2xyz(self.lat[j]*rad, self.lon[j]*rad, self.elev[j])
                # Compute distance between stations
                stat_dist[j] = np.linalg.norm(ref_X - curr_X)
            if L0 is None:
                # Mean distance to 3 nearest neighbors multipled by a smoothing factor
                Lc = smooth * np.mean(np.sort(stat_dist)[1:1+n_neighbor])
                dist_weight[i,:] = np.exp(-stat_dist / Lc)
                print(' - scale length at', self.names[i], ':', 0.001 * Lc, 'km')
            else:
                dist_weight[i,:] = np.exp(-stat_dist / L0)

        return dist_weight


    def preprocess(self, engine_out):
        """
        Read header from saved file list to remove offsets from time series.
        """
        # Import necessary GIAnT utilities
        import tsinsar as ts
        import tsinsar.sopac as sopac

        # Initialize data frames for each components
        frames = {}
        for component in self.inst.components:
            frames[component] = None

        # Loop over the files
        for filepath in self.engine.getUniqueFiles():

            fname = filepath.split('/')[-1]
            statid = fname[:4]
            print('Cleaning station', statid)
            
            # Create sopac model to read the header 
            smodel = sopac.sopac(filepath)

            # Loop over the components
            for component in self.inst.components:
        
                # Get the data
                data = self.get(component, statid, with_date=True)

                # Remove offsets if applicable
                frep = getattr(smodel, component).offset
                if len(frep) > 1:
                    # Get representations and amps
                    rep = [crep.rep for crep in frep]
                    amp = [crep.amp for crep in frep]

                    # Construct model and remove from data
                    G = ts.Timefn(rep, self.tdec)[0]
                    fit = np.dot(G, amp)
                    data[statid] -= fit

                # Merge results
                if frames[component] is None:
                    frames[component] = data
                else:
                    frames[component] = pd.merge(frames[component], data,
                        how='outer', on='DATE')

        # Write to SQL database
        for component in self.inst.components:
            # Write data
            frames[component].to_sql(component, engine_out.engine, if_exists='replace')
            # Read and write sigmas
            sigma_df = pd.read_sql_table('sigma_' + component, self.engine.engine)
            sigma_df.to_sql('sigma_' + component, engine_out.engine, if_exists='replace')

        return


    def filterData(self, engine_out, kernel_size, mask=False, remove_outliers=False,
        nstd=5, std_thresh=100.0):
        """
        Call median filter function.
        """
        from progressbar import ProgressBar, Bar, Percentage

        if kernel_size % 2 == 0:
            kernel_size += 1
        print('Window of integer size', kernel_size)

        # Loop over the data
        for component in self.inst.components:

            # Get data for this component
            comp_df = self.get(component, None, with_date=True)
            N = comp_df.shape[0]

            # Make a copy for filtered results
            filt_df = comp_df.copy()

            # Make a progress bar for the stations
            keep_stat = []
            pbar = ProgressBar(widgets=[Percentage(), Bar()], maxval=self.nstat).start()
            for scnt, statname in enumerate(self.names):
                # Get the data
                data = comp_df[statname]
                # Skip if all NaN
                if data.isnull().sum() == N:
                    continue
                # Filter
                filtered = self.adaptiveMedianFilt(data.values, kernel_size)
                # mask
                if mask:
                    filtered[np.isnan(data)] = np.nan
                # Remove outliers
                if remove_outliers:
                    residual = data.values - filtered
                    std = np.nanstd(residual)
                    if std > std_thresh:
                        continue
                    comp_df.loc[residual > 5*std,statname] = np.nan
                filt_df[statname] = filtered                
                keep_stat.append(statname)
                pbar.update(scnt + 1)
            pbar.finish()

            # Update metadata if list of good stations has changed
            self.updateMetadata(keep_stat, engine_out)

            # Write data to database
            keep_stat = ['DATE'] + keep_stat
            comp_df[keep_stat].to_sql(component, engine_out.engine, if_exists='replace')
            filt_df[keep_stat].to_sql('filt_' + component, 
                engine_out.engine, if_exists='replace')

            # Read and write sigmas
            sigma_df = self.get('sigma_' + component, None, with_date=True)
            sigma_df.to_sql('sigma_' + component, engine_out.engine, if_exists='replace')

        return


    def decompose(self, engine_out, n_comp=1, plot=True, method='pca', 
        remove=False):
        """
        Peform principal component analysis on a stack of time series.
        """

        # Create the decomposer
        from sklearn.decomposition import FastICA, PCA
        if method == 'pca':
            decomposer = PCA(n_components=n_comp, whiten=False)
        elif method == 'ica':
            decomposer = FastICA(n_components=n_comp, whiten=True, max_iter=500)
        else:
            raise NotImplementedError('Unsupported decomposition method')
        
        # Now decompose the time series
        temporal = {}; spatial = {}; model = {}
        for component in self.inst.components:

            # Retrieve component data frame
            comp_df = self.get(component, None, with_date=False)
            filt_df = self.get('filt_' + component, None, with_date=False)

            # First loop over the stations to compute residuals; fill
            # gaps with random noise
            for statname in self.names:
                data = comp_df[statname].values
                filt = filt_df[statname].values
                residual = data - filt
                residual -= np.nanmean(residual)
                ind = np.isnan(residual).nonzero()[0]
                residual[ind] = np.nanstd(residual) * np.random.randn(len(ind))
                comp_df[statname] = residual

            # Perform decomposition
            temporal[component] = decomposer.fit_transform(comp_df.values)
            if method == 'pca':
                spatial[component] = decomposer.components_.squeeze()
                model[component] = decomposer.inverse_transform(temporal[component])
            elif method == 'ica':
                spatial[component] = decomposer.mixing_[:,n_comp-1].squeeze()

        if plot:
            import matplotlib.pyplot as plt
            ax1 = plt.subplot2grid((3,2), (0,0))
            ax2 = plt.subplot2grid((3,2), (1,0))
            ax3 = plt.subplot2grid((3,2), (2,0))
            ax4 = plt.subplot2grid((3,2), (0,1), rowspan=3)
            ax1.plot(self.tdec, temporal['east'], '-b')
            ax2.plot(self.tdec, temporal['north'], '-b')
            ax3.plot(self.tdec, temporal['up'], '-b')
            ax4.quiver(self.lon, self.lat, spatial['east'], spatial['north'],
                scale=0.1)
            for lon,lat,name in zip(self.lon,self.lat,self.names):
                ax4.annotate(name, xy=(lon,lat))
            
            plt.show()
            sys.exit()

        if remove:
            for comp in self.components:
                A = model[comp]
                for j, (statname, stat) in enumerate(self.statGen):
                    dat = stat[comp]
                    raw_var = np.nanstd(dat)**2
                    dat -= A[:,j]
                    filt_var = np.nanstd(dat)**2
                    print('%s-%s variance reduction: %f' % (statname, comp, filt_var/raw_var))
        
        return


    def decompose_ALS(self, engine_out, n_comp=1, plot=True, remove=False,
        beta=1.0, max_step=30):
        """
        Peform principal component analysis on a stack of time series.
        """

        from .utils import ALS_factor
        
        # Now decompose the time series
        temporal = {}; spatial = {}; model = {}
        for component in self.inst.components:

            print(' - ALS for %s component' % component)

            # Retrieve component data frame
            comp_df = self.get(component, None, with_date=False)
            filt_df = self.get('filt_' + component, None, with_date=False)
            residual = comp_df.values - filt_df.values
            for j in range(residual.shape[1]):
                residual[:,j] -= np.nanmean(residual[:,j])
            
            # Perform decomposition
            tempMat, spatMat, errors = ALS_factor(residual, beta, 
                num_features=n_comp, max_step=max_step)

            # Save
            temporal[component] = tempMat.squeeze()
            spatial[component] = spatMat.squeeze()
            model[component] = np.dot(tempMat, spatMat.T)
            
        if plot:
            import matplotlib.pyplot as plt
            fig = plt.figure(figsize=(16,10))
            ax1 = plt.subplot2grid((3,2), (0,0))
            ax2 = plt.subplot2grid((3,2), (1,0))
            ax3 = plt.subplot2grid((3,2), (2,0))
            ax4 = plt.subplot2grid((3,2), (0,1), rowspan=3)
            ax1.plot(self.tdec, temporal['east'], '-b')
            ax2.plot(self.tdec, temporal['north'], '-b')
            ax3.plot(self.tdec, temporal['up'], '-b')
            ax4.quiver(self.lon, self.lat, spatial['east'], spatial['north'], scale=10.0)
            for lon,lat,name in zip(self.lon,self.lat,self.names):
                ax4.annotate(name, xy=(lon,lat))
            plt.savefig('results_cme_als.png', dpi=200, bbox_inches='tight')
            #plt.show()
            #sys.exit()

        if remove:
            for component in self.inst.components:
                A = model[component]
                comp_df = self.get(component, None, with_date=True)
                comp_df.to_sql(component, engine_out.engine, if_exists='replace')
                for cnt, statname in enumerate(self.names):
                    comp_df.loc[:,statname] -= A[:,cnt]
                comp_df.to_sql('filt_' + component, engine_out.engine, if_exists='replace')
        
        return


    def residuals(self):
        """
        Compute residuals between component and filt_component.
        """
        for comp in self.components:
            for statname, stat in self.statGen:
                data = stat[comp]
                filtered = stat['filt_' + comp]
                residual = data - filtered
                stat['residual_' + comp] = residual
        return 
 

    @staticmethod
    def adaptiveMedianFilt(dat, kernel_size):
        """
        Perform a median filter with a sliding window. For edges, we shrink window.
        """
        assert kernel_size % 2 == 1, 'kernel_size must be odd'

        # Run filtering while suppressing warnings
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', 'All-NaN slice encountered')

            nobs = dat.size
            filt_data = np.nan * np.ones_like(dat)

            # Beginning region
            halfWindow = 0
            for i in range(kernel_size//2):
                filt_data[i] = nanmedian(dat[i-halfWindow:i+halfWindow+1])
                halfWindow += 1

            # Middle region
            halfWindow = kernel_size // 2
            for i in range(halfWindow, nobs - halfWindow):
                filt_data[i] = nanmedian(dat[i-halfWindow:i+halfWindow+1])

            # Ending region
            halfWindow -= 1
            for i in range(nobs - halfWindow, nobs):
                filt_data[i] = nanmedian(dat[i-halfWindow:i+halfWindow+1])
                halfWindow -= 1

        return filt_data


    def computeStatDistance(self, statname1, statname2):
        """
        Compute the distance between two stations.
        """
        ind1 = (self.name == statname1.lower()).nonzero()[0]
        ind2 = (self.name == statname2.lower()).nonzero()[0]
        assert len(ind1) == 1, 'Cannot find first station'
        assert len(ind2) == 1, 'Cannot find second station'

        # Retrieve lat/lon
        lon1, lat1 = self.lon[ind1[0]], self.lat[ind1[0]]
        lon2, lat2 = self.lon[ind2[0]], self.lat[ind2[0]]

        # Convert to XYZ and compute Cartesian distance
        from topoutil import llh2xyz
        X1 = llh2xyz(lat1, lon1, 0.0, deg=True).squeeze()
        X2 = llh2xyz(lat2, lon2, 0.0, deg=True).squeeze()
        dX = np.linalg.norm(X2 - X1)
        return dX


    @property
    def tstart(self):
        return selt.tdec[0]
    @tstart.setter
    def tstart(self, val):
        raise AttributeError('Cannot set tstart explicitly')

    @property
    def trel(self):
        return self.tdec - self.tdec[0]
    @trel.setter
    def trel(self, val):
        raise AttributeError('Cannot set tstart explicitly')

    @property
    def numObs(self):
        return len(self.tdec)
    @numObs.setter
    def numObs(self, val):
        raise AttributeError('Cannot set numObs explicitly')



class GenericClass:
    pass
