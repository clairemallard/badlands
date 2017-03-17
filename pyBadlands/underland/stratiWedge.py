##~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~##
##                                                                                   ##
##  This file forms part of the Badlands surface processes modelling application.    ##
##                                                                                   ##
##  For full license and copyright information, please refer to the LICENSE.md file  ##
##  located at the project root, or contact the authors.                             ##
##                                                                                   ##
##~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~#~##
"""
This module defines the stratigraphic layers based on the irregular TIN grid.
"""
import os
import glob
import time
import h5py
import numpy
import pandas
import mpi4py.MPI as mpi
from scipy import interpolate
from scipy.spatial import cKDTree
from scipy.interpolate import RegularGridInterpolator
from pyBadlands.libUtils import FASTloop

class stratiWedge():
    """
    This class builds stratigraphic layers over time based on erosion/deposition values.

    Parameters
    ----------
    paleoDepth
        Numpy array containing the elevation at time of deposition (paleo-depth)

    depoThick
        Numpy array containing the thickness of each stratigraphic layer for each type of sediment
    """

    def __init__(self, layNb, elay, xyTIN, bPts, ePts, thickMap, folder, h5file, rockNb,
                 regX, regY, cumdiff=0, rfolder=None, rstep=0):
        """
        Constructor.

        Parameters
        ----------
        layNb
            Total number of stratigraphic layers

        xyTIN
            Numpy float-type array containing the coordinates for each nodes in the TIN (in m)

        bPts : integer
            Boundary points for the TIN.

        ePts : integer
            Boundary points for the regular grid.

        thickMap : file array
            Filename containing initial layer parameters

        folder
            Name of the output folder.

        h5file
            First part of the hdf5 file name.

        rockNb
            Number of type of sedimentary rocks represented in the current simulation.

        cumdiff
            Numpy array containing cumulative erosion/deposition from previous simulation.

        regX : float
            Numpy array containing the X-coordinates of the regular input grid.

        regY : float
            Numpy array containing the Y-coordinates of the regular input grid.

        rfolder
            Restart folder.

        rstep
            Restart step.
        """

        # Initialise MPI communications
        comm = mpi.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()

        # Number of points on the TIN
        self.ptsNb = len(xyTIN)
        self.oldload = None
        self.folder = folder
        self.h5file = h5file
        self.rockNb = rockNb

        # In case we restart a simulation
        if rstep > 0:
            if os.path.exists(rfolder):
                folder = rfolder+'/h5/'
                fileCPU = 'stratal.time%s.p*.hdf5'%rstep
                restartncpus = len(glob.glob1(folder,fileCPU))
                if restartncpus == 0:
                    raise ValueError('The requested time step for the restart simulation cannot be found in the restart folder.')
            else:
                raise ValueError('The restart folder is missing or the given path is incorrect.')

            if restartncpus != size:
                raise ValueError('When using the stratal model you need to run the restart simulation with the same number of processors as the previous one.')

            df = h5py.File('%s/h5/stratal.time%s.p%s.hdf5'%(rfolder, rstep, rank), 'r')

            paleoDepth = numpy.array((df['/paleoDepth']))
            eroLay =  paleoDepth.shape[1]
            self.step = paleoDepth.shape[1]

            # Elevation at time of deposition (paleo-depth)
            self.paleoDepth = numpy.zeros([self.ptsNb,layNb+eroLay])
            self.paleoDepth[:,:eroLay] = paleoDepth
            # Deposition thickness for each type of sediment
            self.depoThick = numpy.zeros([self.ptsNb,layNb+eroLay,rockNb])
            for r in range(rockNb):
                self.depoThick[:,:eroLay,r] = numpy.array((df['/depoThickRock'+str(r)]))
            # Define cumulative deposition/erosion from previous simulation
            self.oldload = cumdiff
        else:
            eroLay = elay+1
            self.step = eroLay

            tmpTH = numpy.zeros(self.ptsNb)
            # Elevation at time of deposition (paleo-depth)
            self.paleoDepth = numpy.zeros((self.ptsNb,layNb+eroLay))
            # Deposition thickness for each type of sediment
            self.depoThick = numpy.zeros((self.ptsNb,layNb+eroLay,rockNb))
            # Rock type array
            rockType = -numpy.ones(self.ptsNb,dtype=int)

            # If predefined layers exists
            if elay > 0:
                # Build the underlying erodibility mesh and associated thicknesses

                # Define inside area kdtree
                inTree = cKDTree(xyTIN[bPts:ePts+bPts,:])
                dist, inID = inTree.query(xyTIN[:bPts,:],k=1)
                inID += bPts

                # Data is stored from top predefined layer to bottom.
                for l in range(1,eroLay):
                    thMap = pandas.read_csv(str(thickMap[l-1]), sep=r'\s+', engine='c',
                                       header=None, na_filter=False, dtype=numpy.float, low_memory=False)
                    # Extract thickness values
                    tmpH = thMap.values[:,0]
                    tH = numpy.reshape(tmpH,(len(regX), len(regY)), order='F')
                    # Nearest neighbours interpolation to extract rock type values
                    tmpS = thMap.values[:,1].astype(int)
                    tS = numpy.reshape(tmpS,(len(regX), len(regY)), order='F')
                    rockType[bPts:] = interpolate.interpn( (regX, regY), tS, xyTIN[bPts:,:], method='nearest')
                    # Linear interpolation to define underlying layers on the TIN
                    tmpTH.fill(0.)
                    tmpTH[bPts:] = interpolate.interpn( (regX, regY), tH, xyTIN[bPts:,:], method='linear')
                    for r in range(rockNb):
                        ids = numpy.where(numpy.logical_and(rockType==r,tmpTH>0.))
                        self.depoThick[ids,eroLay-l,r] = tmpTH[ids]
                        if eroLay-l==1:
                            self.depoThick[ids,0,r] = 1.e6
                        # Add an infinite rock layer with the same characteristics as the deepest one
                        self.depoThick[:bPts,eroLay-l,r] = self.depoThick[inID,eroLay-l,r]
                        if eroLay-l==1:
                            ids = numpy.where(self.depoThick[:bPts,eroLay-l,r]>0)[0]
                            self.depoThick[ids,0,r] = 1.e6
            else:
                # Add an infinite rock layer with the same characteristics as the deepest one
                self.depoThick[:,0,0] = 1.e6

    def write_hdf5_stratigraphy(self, outstep, rank):
        """
        This function writes for each processor the HDF5 file containing sub-surface information.

        Parameters
        ----------
        outstep
            Output time step.

        rank
            ID of the local partition.
        """

        sh5file = self.folder+'/'+self.h5file+str(outstep)+'.p'+str(rank)+'.hdf5'
        with h5py.File(sh5file, "w") as f:

            # Write stratal layers paeleoelevations per cells
            f.create_dataset('paleoDepth',shape=(self.ptsNb,self.step), dtype='float32', compression='gzip')
            f["paleoDepth"][:,:self.step] = self.paleoDepth[:,:self.step]

            # Write stratal layers thicknesses per cells
            for r in range(self.rockNb):
                f.create_dataset('depoThickRock'+str(r),shape=(self.ptsNb,self.step), dtype='float32', compression='gzip')
                f['depoThickRock'+str(r)][:,:self.step] = self.depoThick[:,:self.step,r]