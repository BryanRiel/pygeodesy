#-*- coding: utf-8 -*-

import numpy as np
import matplotlib.pyplot as plt
from mpi4py import MPI
import datetime as dtime
import tsinsar as ts
import h5py
import sys
import os

from timeutils import datestr2tdec
from .TimeSeries import TimeSeries
from .StationGenerator import StationGenerator


class Insar(TimeSeries):
    """
    Class to hold well stations.
    """

    def __init__(self, name='insar', stnfile=None, stnlist=None, comm=None):
        """
        Initiate Insar class.
        """
        # Initialize the TimeSeries parent class
        super().__init__(name=name, stnfile=stnfile, dtype='insar')
        # Save some MPI parameters
        self.comm = comm or MPI.COMM_WORLD
        self.rank = self.comm.Get_rank()
        self.n_workers = self.comm.Get_size()
        return


    def loadStationH5(self, h5file, fileout=None, copydict=False):
        """
        Transfers data from a GIAnT formatted stack to self.
        """
        self.clear()

        # Only master worker will have access to underlying data
        if self.rank == 0:

            # Open the H5 file and store access to igrams or time series and weights
            self.h5file = h5py.File(h5file, 'r')
            try:
                # Load interferograms
                self._igram = self.h5file['igram']
                self._data = self._igram
                self.tdec = self.h5file['tdec'].value
                self.tinsar = self.h5file['tinsar'].value
                self.Jmat = self.h5file['Jmat'].value
            except KeyError:
                # Or load time series
                self._recon = self.h5file['recon']
                self._data = self._recon 
                self.tdec = self.h5file['tdec'].value
            self._weights = self.h5file['weights']

            # Instantiate a custom station generator with arrays
            self.statGen = StationGenerator(self._data, self._weights, 
                self.h5file['lat'].value, self.h5file['lon'].value, self.h5file['elev'].value)

            # And make a dictionary that just links to the generator
            self.statDict = self.statGen

            # Also chunk geometry
            self.chunk_shape = self.h5file['chunk_shape'].value
            self.data_shape = self._data.shape

        else:
            self.tdec = self.Jmat = self.chunk_shape = self.data_shape = None

        # Broadcast some useful variables to the workers
        self.tdec = self.comm.bcast(self.tdec, root=0)
        self.Jmat = self.comm.bcast(self.Jmat, root=0)
        self.chunk_shape = self.comm.bcast(self.chunk_shape, root=0)
        self.data_shape = self.comm.bcast(self.data_shape, root=0)
        self.Ny, self.Nx = self.data_shape[1:]
        self.nstat = self.Ny * self.Nx

        return


    def initialize(self, data_shape, tdec, statGen=None, chunk_size=128, 
        filename='outputInsarStack.h5', access_mode='w', recon=False):
        """
        Initialize output arrays in H5 format.
        
        Parameters
        ----------
        data_shape: list or tuple
            Specifies the 3D shape of the interferogram array.
        tdec: ndarray
            Array of observation epochs in decimal year.
        statGen: {None, StationGenerator}, optional
            If provided, get lat/lon/elev from object.
        chunk_size: int, optional
            The size of the chunk chip for H5. Default: 128.
        filename: str, optional
            Output H5 filename. Default: 'outputInsarStack.h5'
        access_mode: str {'w', 'r'}, optional
            Access mode of the H5 arrays. Default: 'w' for write.
        recon: bool, optional
            The output data is reconstructed time series, instead of igram (False).
        """
        # Initialize variables common to all workers
        self.chunk_shape = [chunk_size, chunk_size]
        h5_chunk_shape = (1,chunk_size,chunk_size)
        self.recon = recon
        self.tdec = tdec
        self.Ny, self.Nx = data_shape[1:]
        self.nstat = self.Ny * self.Nx

        # Only master worker will have access to underlying data
        if self.rank == 0:

            # Open the H5 file and initialize data sets
            self.h5file = h5py.File(filename, access_mode)
            if self.recon:
                self._recon = self.h5file.create_dataset('recon', shape=data_shape, 
                    dtype=np.float32, chunks=h5_chunk_shape)
                self._data = self._recon
            else:
                self._igram = self.h5file.create_dataset('igram', shape=data_shape, 
                    dtype=np.float32, chunks=h5_chunk_shape)
                self._data = self._igram
            self._weights = self.h5file.create_dataset('weights', shape=data_shape, 
                dtype=np.float32, chunks=h5_chunk_shape)

            # Make a station generator
            if isinstance(statGen, StationGenerator):
                self.statGen = statGen
                self.statGen.los = self._data
                self.statGen.w_los = self._weights
                # Also make lat/lon/elev arrays in output H5
                for key in ['lat', 'lon', 'elev']:
                    self.h5file[key] = getattr(self.statGen, key)
            else:
                self.statGen = StationGenerator(self._igram, self._weights)

            ## And make a dictionary that just links to the generator
            self.statDict = self.statGen

            # Make sure we save tdec and 'insar' data type
            self.h5file['tdec'] = tdec
            self.h5file['dtype'] = 'insar'
            self.h5file['chunk_shape'] = self.chunk_shape

        # Barrier for safeguard
        self.comm.Barrier()
        return


    def getChunk(self, slice_y, slice_x, dtype='igram'):
        """
        Loads H5 data for a specified chunk given by slice objects.

        Parameters
        ----------
        slice_y: slice
            Slice of array in vertical dimension.
        slice_x: slice
            Slice of array in horizontal dimension.
        dtype: str {'igram', 'weight', 'par', 'recon'}, optional
            A string indicating which array to get the slice from:

            ``igram``
                The interferogram array. (Default)
            ``weight``
                The weight array (1 / sigma).
            ``par``
                The time series parameter array.
            ``recon``
                The reconstructed time series.

        Returns
        -------
        x: ndarray
            Array of data corresponding to specified chunk.
        """
        # Dictionary mapping dtype to attribute
        attr_dict = {'igram': '_igram', 'weight': '_weights',
            'par': '_par', 'recon': '_recon'}

        # Load data
        if self.rank == 0:
            arr = getattr(self, attr_dict[dtype])
            x = arr[:,slice_y,slice_x]
            x_shape = x.shape
        else:
            x_shape = None

        # Broadcast it
        x_shape = self.comm.bcast(x_shape, root=0)
        if self.rank != 0:
            x = np.empty(x_shape, dtype=np.float32)
        self.comm.Bcast([x, MPI.FLOAT], root=0)

        return x


    def setChunk(self, dat, slice_y, slice_x, dtype='igram', verbose=False):
        """
        Saves H5 data for a specified chunk given by slice objects.

        Parameters
        ----------
        dat: ndarray
            3D chunk array to save.
        slice_y: slice
            Slice of array in vertical dimension.
        slice_x: slice
            Slice of array in horizontal dimension.
        dtype: str {'igram', 'weight', 'par', 'recon'}, optional
            A string indicating which array to get the slice from:

            ``igram``
                The interferogram array. (Default)
            ``weight``
                The weight array (1 / sigma).
            ``par``
                The time series parameter array.
            ``recon``
                The reconstructed time series.
        verbose: bool, optional
            Print some statement. Default: False.
        """
        # Dictionary mapping dtype to attribute
        attr_dict = {'igram': '_igram', 'weight': '_weights',
            'par': '_par', 'recon': '_recon'}

        # Save data
        if self.rank == 0:
            if verbose: print('Saving chunk', (slice_y, slice_x))
            arr = getattr(self, attr_dict[dtype])
            arr[:,slice_y,slice_x] = dat
        return 

        
    def loadSeasonalH5(self, h5file):
        """
        Transfers data from an h5py data stack to self. For Insar object, we
        simply save the underlying data array.
        """
        # Open the file and read the data
        self.seasonal_fid = h5py.File(h5file, 'r')
        self.statGen.seasm_los = self.seasonal_fid['seasm_los']
        # Get the number of periodic B-splines
        self.npbspline = seas_dat['npbspline']
        self.npar_seasonal = self.seasonal_fid['seasm_los'].shape[0]
        # Remember that we have seasonal data
        self.have_seasonal = True
        return


# end of file