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

"""gprMax.gprMax: provides entry point main()."""

# Set the version number here
__version__ = '3.0.0b16'
versionname = ' (Bowmore)'

import argparse, datetime, importlib, itertools, os, psutil, sys
from time import perf_counter
from copy import deepcopy
from enum import Enum
from collections import OrderedDict

import numpy as np

from gprMax.constants import c, e0, m0, z0, floattype
from gprMax.exceptions import CmdInputError
from gprMax.fields_update import *
from gprMax.grid import FDTDGrid
from gprMax.input_cmds_geometry import process_geometrycmds
from gprMax.input_cmds_file import python_code_blocks, write_python_processed, check_cmd_names
from gprMax.input_cmds_multiuse import process_multicmds
from gprMax.input_cmds_singleuse import process_singlecmds
from gprMax.materials import Material
from gprMax.output import prepare_output_file, write_output
from gprMax.pml_call_updates import update_electric_pml, update_magnetic_pml
from gprMax.pml import build_pml, calculate_initial_pml_params
from gprMax.utilities import update_progress, logo, human_size
from gprMax.yee_cell_build import build_ex_component, build_ey_component, build_ez_component, build_hx_component, build_hy_component, build_hz_component


def main():
    """This is the main function for gprMax."""
    
    # Print gprMax logo, version, and licencing/copyright information
    logo(__version__ + versionname)

    # Parse command line arguments
    parser = argparse.ArgumentParser(prog='gprMax', description='Electromagnetic modelling software based on the Finite-Difference Time-Domain (FDTD) method')
    parser.add_argument('inputfile', help='path to and name of inputfile')
    parser.add_argument('-n', default=1, type=int, help='number of times to run the input file')
    parser.add_argument('-mpi', action='store_true', default=False, help='switch on MPI')
    parser.add_argument('--geometry-only', action='store_true', default=False, help='only build model and produce geometry file(s)')
    parser.add_argument('--write-python', action='store_true', default=False, help='write an input file after any Python code blocks in the original input file have been processed')
    parser.add_argument('--opt-taguchi', action='store_true', default=False, help='optimise parameters using the Taguchi optimisation method')
    args = parser.parse_args()
    numbermodelruns = args.n
    inputdirectory = os.path.dirname(os.path.abspath(args.inputfile)) + os.sep
    inputfile = inputdirectory + os.path.basename(args.inputfile)
    inputfileparts = os.path.splitext(inputfile)
    
    # Create a separate namespace that users can access in any Python code blocks in the input file
    usernamespace = {'c': c, 'e0': e0, 'm0': m0, 'z0': z0, 'number_model_runs': numbermodelruns, 'inputdirectory': inputdirectory}
    
    if args.opt_taguchi and numbermodelruns > 1:
        raise CmdInputError('When a Taguchi optimisation is being carried out the number of model runs argument is not required')

    ########################################
    #   Process for Taguchi optimisation   #
    ########################################
    if args.opt_taguchi:
        from user_libs.optimisations.taguchi import taguchi_code_blocks, select_OA, calculate_ranges_experiments, calculate_optimal_levels, plot_optimisation_history

        # Default maximum number of iterations of optimisation to perform (used if the stopping criterion is not achieved)
        maxiterations = 20
        
        # Process Taguchi code blocks in the input file; pass in ordered dictionary to hold parameters to optimise
        tmp = usernamespace.copy()
        tmp.update({'optparams': OrderedDict()})
        taguchinamespace = taguchi_code_blocks(inputfile, tmp)
        
        # Extract dictionaries and variables containing initialisation parameters
        optparams = taguchinamespace['optparams']
        fitness = taguchinamespace['fitness']
        if 'maxiterations' in taguchinamespace:
            maxiterations = taguchinamespace['maxiterations']

        # Store initial parameter ranges
        optparamsinit = list(optparams.items())

        # Dictionary to hold history of optmised values of parameters
        optparamshist = OrderedDict((key, list()) for key in optparams)
        
        # Import specified fitness function
        fitness_metric = getattr(importlib.import_module('user_libs.optimisations.taguchi_fitness'), fitness['name'])

        # Select OA
        OA, N, k, s = select_OA(optparams)
        
        # Initialise arrays and lists to store parameters required throughout optimisation
        # Lower, central, and upper values for each parameter
        levels = np.zeros((s, k), dtype=floattype)
        # Optimal lower, central, or upper value for each parameter
        levelsopt = np.zeros(k, dtype=floattype)
        # Difference used to set values for levels
        levelsdiff = np.zeros(k, dtype=floattype)
        # History of fitness values from each confirmation experiment
        fitnessvalueshist = []

        i = 0
        while i < maxiterations:
            # Set number of model runs to number of experiments
            numbermodelruns = N
            usernamespace['number_model_runs'] = numbermodelruns
            
            # Fitness values for each experiment
            fitnessvalues = []
    
            # Set parameter ranges and define experiments
            optparams, levels, levelsdiff = calculate_ranges_experiments(optparams, optparamsinit, levels, levelsopt, levelsdiff, OA, N, k, s, i)
    
            # Mixed mode MPI/OpenMP - task farm for model runs with MPI; each model parallelised with OpenMP
            if args.mpi:        
                from mpi4py import MPI

                # Define MPI message tags
                tags = Enum('tags', {'READY': 0, 'DONE': 1, 'EXIT': 2, 'START': 3})

                # Initializations and preliminaries
                comm = MPI.COMM_WORLD   # get MPI communicator object
                size = comm.size        # total number of processes
                rank = comm.rank        # rank of this process
                status = MPI.Status()   # get MPI status object
                name = MPI.Get_processor_name()     # get name of processor/host

                if rank == 0:
                    # Master process
                    modelrun = 1
                    numworkers = size - 1
                    closedworkers = 0
                    print('Master: PID {} on {} using {} workers.'.format(os.getpid(), name, numworkers))
                    while closedworkers < numworkers:
                        data = comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
                        source = status.Get_source()
                        tag = status.Get_tag()
                        if tag == tags.READY.value:
                            # Worker is ready, so send it a task
                            if modelrun < numbermodelruns + 1:
                                comm.send(modelrun, dest=source, tag=tags.START.value)
                                print('Master: sending model {} to worker {}.'.format(modelrun, source))
                                modelrun += 1
                            else:
                                comm.send(None, dest=source, tag=tags.EXIT.value)
                        elif tag == tags.DONE.value:
                            print('Worker {}: completed.'.format(source))
                        elif tag == tags.EXIT.value:
                            print('Worker {}: exited.'.format(source))
                            closedworkers += 1
                else:
                    # Worker process
                    print('Worker {}: PID {} on {} requesting {} OpenMP threads.'.format(rank, os.getpid(), name, os.environ.get('OMP_NUM_THREADS')))
                    while True:
                        comm.send(None, dest=0, tag=tags.READY.value)
                        # Receive a model number to run from the master
                        modelrun = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                        tag = status.Get_tag()
                        
                        if tag == tags.START.value:
                            # Run a model
                            # Add specific value for each parameter to optimise for each experiment to user accessible namespace
                            optnamespace = usernamespace.copy()
                            tmp = {}
                            tmp.update((key, value[modelrun - 1]) for key, value in optparams.items())
                            optnamespace.update({'optparams': tmp})
                            run_model(args, modelrun, numbermodelruns, inputfile, usernamespace)
                            comm.send(None, dest=0, tag=tags.DONE.value)
                        elif tag == tags.EXIT.value:
                            break

                    comm.send(None, dest=0, tag=tags.EXIT.value)

            # Standard behaviour - models run serially; each model parallelised with OpenMP
            else:
                tsimstart = perf_counter()
                for modelrun in range(1, numbermodelruns + 1):
                    # Add specific value for each parameter to optimise, for each experiment to user accessible namespace
                    optnamespace = usernamespace.copy()
                    tmp = {}
                    tmp.update((key, value[modelrun - 1]) for key, value in optparams.items())
                    optnamespace.update({'optparams': tmp})
                    run_model(args, modelrun, numbermodelruns, inputfile, optnamespace)
                tsimend = perf_counter()
                print('\nTotal simulation time [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tsimend - tsimstart))))

            # Calculate fitness value for each experiment
            for exp in range(1, numbermodelruns + 1):
                outputfile = inputfileparts[0] + str(exp) + '.out'
                fitnessvalues.append(fitness_metric(outputfile, fitness['args']))
                os.remove(outputfile)

            print('\nTaguchi optimisation, iteration {}: completed initial {} experiments completed with fitness values {}.'.format(i + 1, numbermodelruns, fitnessvalues))
            
            # Calculate optimal levels from fitness values by building a response table; update dictionary of parameters with optimal values
            optparams, levelsopt = calculate_optimal_levels(optparams, levels, levelsopt, fitnessvalues, OA, N, k)

            # Run a confirmation experiment with optimal values
            numbermodelruns = 1
            usernamespace['number_model_runs'] = numbermodelruns
            tsimstart = perf_counter()
            for modelrun in range(1, numbermodelruns + 1):
                # Add specific value for each parameter to optimise, for each experiment to user accessible namespace
                optnamespace = usernamespace.copy()
                tmp = {}
                for key, value in optparams.items():
                    tmp[key] = value[modelrun - 1]
                    optparamshist[key].append(value[modelrun - 1])
                optnamespace.update({'optparams': tmp})
                run_model(args, modelrun, numbermodelruns, inputfile, optnamespace)
            tsimend = perf_counter()
            print('\nTotal simulation time [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tsimend - tsimstart))))

            # Calculate fitness value for confirmation experiment
            outputfile = inputfileparts[0] + '.out'
            fitnessvalueshist.append(fitness_metric(outputfile, fitness['args']))

            # Rename confirmation experiment output file so that it is retained for each iteraction
            os.rename(outputfile, os.path.splitext(outputfile)[0] + '_final' + str(i + 1) + '.out')
            
            print('\nTaguchi optimisation, iteration {} completed. History of optimal parameter values {} and of fitness values {}'.format(i + 1, dict(optparamshist), fitnessvalueshist, 68*'*'))
            
            i += 1

            # Stop optimisation if stopping criterion has been reached
            if fitnessvalueshist[i - 1] > fitness['stop']:
                break

            # Stop optimisation if successive fitness values are within 1%
            if i > 2:
                fitnessvaluesclose = (np.abs(fitnessvalueshist[i - 2] - fitnessvalueshist[i - 1]) / fitnessvalueshist[i - 1]) * 100
                if fitnessvaluesclose < 1:
                    break

        # Save optimisation parameters history and fitness values history to file
        opthistfile = inputfileparts[0] + '_hist'
        np.savez(opthistfile, dict(optparamshist), fitnessvalueshist)

        print('\n{}\nTaguchi optimisation completed after {} iteration(s).\nHistory of optimal parameter values {} and of fitness values {}\n{}\n'.format(68*'*', i, dict(optparamshist), fitnessvalueshist, 68*'*'))

        # Plot the history of fitness values and each optimised parameter values for the optimisation
        plot_optimisation_history(fitnessvalueshist, optparamshist, optparamsinit)


    #######################################
    #   Process for standard simulation   #
    #######################################
    else:
        if args.mpi and numbermodelruns == 1:
            raise CmdInputError('MPI is not beneficial when there is only one model to run')

        # Mixed mode MPI/OpenMP - task farm for model runs with MPI; each model parallelised with OpenMP
        if args.mpi:
            from mpi4py import MPI

            # Define MPI message tags
            tags = Enum('tags', {'READY': 0, 'DONE': 1, 'EXIT': 2, 'START': 3})

            # Initializations and preliminaries
            comm = MPI.COMM_WORLD   # get MPI communicator object
            size = comm.size        # total number of processes
            rank = comm.rank        # rank of this process
            status = MPI.Status()   # get MPI status object
            name = MPI.Get_processor_name()     # get name of processor/host

            if rank == 0:
                # Master process
                modelrun = 1
                numworkers = size - 1
                closedworkers = 0
                print('Master: PID {} on {} using {} workers.'.format(os.getpid(), name, numworkers))
                while closedworkers < numworkers:
                    data = comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
                    source = status.Get_source()
                    tag = status.Get_tag()
                    if tag == tags.READY.value:
                        # Worker is ready, so send it a task
                        if modelrun < numbermodelruns + 1:
                            comm.send(modelrun, dest=source, tag=tags.START.value)
                            print('Master: sending model {} to worker {}.'.format(modelrun, source))
                            modelrun += 1
                        else:
                            comm.send(None, dest=source, tag=tags.EXIT.value)
                    elif tag == tags.DONE.value:
                        print('Worker {}: completed.'.format(source))
                    elif tag == tags.EXIT.value:
                        print('Worker {}: exited.'.format(source))
                        closedworkers += 1
            else:
                # Worker process
                print('Worker {}: PID {} on {} requesting {} OpenMP threads.'.format(rank, os.getpid(), name, os.environ.get('OMP_NUM_THREADS')))
                while True:
                    comm.send(None, dest=0, tag=tags.READY.value)
                    # Receive a model number to run from the master
                    modelrun = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                    tag = status.Get_tag()
                    
                    if tag == tags.START.value:
                        # Run a model
                        run_model(args, modelrun, numbermodelruns, inputfile, usernamespace)
                        comm.send(None, dest=0, tag=tags.DONE.value)
                    elif tag == tags.EXIT.value:
                        break

                comm.send(None, dest=0, tag=tags.EXIT.value)

        # Standard behaviour - models run serially; each model parallelised with OpenMP
        else:
            tsimstart = perf_counter()
            for modelrun in range(1, numbermodelruns + 1):
                run_model(args, modelrun, numbermodelruns, inputfile, usernamespace)
            tsimend = perf_counter()
            print('\nTotal simulation time [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tsimend - tsimstart))))

        print('\nSimulation completed.\n{}\n'.format(68*'*'))


def run_model(args, modelrun, numbermodelruns, inputfile, usernamespace):
    """Runs a model - processes the input file; builds the Yee cells; calculates update coefficients; runs main FDTD loop.
        
    Args:
        args (dict): Namespace with command line arguments
        modelrun (int): Current model run number.
        numbermodelruns (int): Total number of model runs.
        inputfile (str): Name of the input file to open.
        usernamespace (dict): Namespace that can be accessed by user in any Python code blocks in input file.
    """
    
    # Monitor memory usage
    p = psutil.Process()
    
    print('\n{}\n\nModel input file: {}\n'.format(68*'*', inputfile))
    
    # Add the current model run to namespace that can be accessed by user in any Python code blocks in input file
    usernamespace['current_model_run'] = modelrun
    print('Constants/variables available for Python scripting: {}\n'.format(usernamespace))
    
    # Process any user input Python commands
    processedlines = python_code_blocks(inputfile, usernamespace)
    
    # Write a file containing the input commands after Python blocks have been processed
    if args.write_python:
        write_python_processed(inputfile, modelrun, numbermodelruns, processedlines)
    
    # Check validity of command names & that essential commands are present
    singlecmds, multicmds, geometry = check_cmd_names(processedlines)

    # Initialise an instance of the FDTDGrid class
    G = FDTDGrid()
    G.inputdirectory = usernamespace['inputdirectory']

    # Process parameters for commands that can only occur once in the model
    process_singlecmds(singlecmds, multicmds, G)

    # Process parameters for commands that can occur multiple times in the model
    process_multicmds(multicmds, G)

    # Initialise an array for volumetric material IDs (solid), boolean arrays for specifying materials not to be averaged (rigid),
    # an array for cell edge IDs (ID), and arrays for the field components.
    G.initialise_std_arrays()

    # Process the geometry commands in the order they were given
    tinputprocstart = perf_counter()
    process_geometrycmds(geometry, G)
    tinputprocend = perf_counter()
    print('\nInput file processed in [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tinputprocend - tinputprocstart))))

    # Build the PML and calculate initial coefficients
    build_pml(G)
    calculate_initial_pml_params(G)

    # Build the model, i.e. set the material properties (ID) for every edge of every Yee cell
    tbuildstart = perf_counter()
    build_ex_component(G.solid, G.rigidE, G.ID, G)
    build_ey_component(G.solid, G.rigidE, G.ID, G)
    build_ez_component(G.solid, G.rigidE, G.ID, G)
    build_hx_component(G.solid, G.rigidH, G.ID, G)
    build_hy_component(G.solid, G.rigidH, G.ID, G)
    build_hz_component(G.solid, G.rigidH, G.ID, G)
    tbuildend = perf_counter()
    print('\nModel built in [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tbuildend - tbuildstart))))

    # Process any voltage sources that have resistance to create a new material at the source location
    #  that adds the voltage source conductivity to the underlying parameters
    if G.voltagesources:
        for source in G.voltagesources:
            if source.resistance != 0:
                if source.polarisation == 'x':
                    requirednumID = G.ID[0, source.positionx, source.positiony, source.positionz]
                    material = next(x for x in G.materials if x.numID == requirednumID)
                    newmaterial = deepcopy(material)
                    newmaterial.ID = material.ID + '|VoltageSource_' + str(source.resistance)
                    newmaterial.numID = len(G.materials)
                    newmaterial.se += G.dx / (source.resistance * G.dy * G.dz)
                    newmaterial.average = False
                    G.ID[0, source.positionx, source.positiony, source.positionz] = newmaterial.numID
                elif source.polarisation == 'y':
                    requirednumID = G.ID[1, source.positionx, source.positiony, source.positionz]
                    material = next(x for x in G.materials if x.numID == requirednumID)
                    newmaterial = deepcopy(material)
                    newmaterial.ID = material.ID + '|VoltageSource_' + str(source.resistance)
                    newmaterial.numID = len(G.materials)
                    newmaterial.se += G.dy / (source.resistance * G.dx * G.dz)
                    newmaterial.average = False
                    G.ID[1, source.positionx, source.positiony, source.positionz] = newmaterial.numID
                elif source.polarisation == 'z':
                    requirednumID = G.ID[2, source.positionx, source.positiony, source.positionz]
                    material = next(x for x in G.materials if x.numID == requirednumID)
                    newmaterial = deepcopy(material)
                    newmaterial.ID = material.ID + '|VoltageSource_' + str(source.resistance)
                    newmaterial.numID = len(G.materials)
                    newmaterial.se += G.dz / (source.resistance * G.dx * G.dy)
                    newmaterial.average = False
                    G.ID[2, source.positionx, source.positiony, source.positionz] = newmaterial.numID
                G.materials.append(newmaterial)

    # Initialise arrays for storing temporary values if there are any dispersive materials
    if Material.maxpoles != 0:
        G.initialise_dispersive_arrays(len(G.materials))
    
    # Initialise arrays of update coefficients to pass to update functions
    G.initialise_std_updatecoeff_arrays(len(G.materials))

    # Calculate update coefficients, store in arrays, and list materials in model
    if G.messages:
        print('\nMaterials:\n')
        print('ID\tName\t\tProperties')
        print('{}'.format('-'*50))
    for x, material in enumerate(G.materials):
        material.calculate_update_coeffsE(G)
        material.calculate_update_coeffsH(G)
        
        G.updatecoeffsE[x, :] = material.CA, material.CBx, material.CBy, material.CBz, material.srce
        G.updatecoeffsH[x, :] = material.DA, material.DBx, material.DBy, material.DBz, material.srcm
        
        if Material.maxpoles != 0:
            z = 0
            for y in range(Material.maxpoles):
                G.updatecoeffsdispersive[x, z:z+3] = e0 * material.eqt2[y], material.eqt[y], material.zt[y]
                z += 3
        
        if G.messages:
            if material.deltaer and material.tau:
                tmp = 'delta_epsr={:g}, tau={:g} secs; '.format(','.join('%g' % deltaer for deltaer in material.deltaer), ','.join('%g' % tau for tau in material.tau))
            else:
                tmp = ''
            if material.average:
                dielectricsmoothing = 'dielectric smoothing permitted.'
            else:
                dielectricsmoothing = 'dielectric smoothing not permitted.'
            print('{:3}\t{:12}\tepsr={:g}, sig={:g} S/m; mur={:g}, sig*={:g} S/m; '.format(material.numID, material.ID, material.er, material.se, material.mr, material.sm) + tmp + dielectricsmoothing)
    
    # Write files for any geometry views
    if G.geometryviews:
        tgeostart = perf_counter()
        for geometryview in G.geometryviews:
            geometryview.write_file(modelrun, numbermodelruns, G)
        tgeoend = perf_counter()
        print('\nGeometry file(s) written in [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tgeoend - tgeostart))))

    # Run simulation if not doing only geometry
    if not args.geometry_only:
        
        # Prepare any snapshot files
        if G.snapshots:
            for snapshot in G.snapshots:
                snapshot.prepare_file(modelrun, numbermodelruns, G)

        # Prepare output file
        inputfileparts = os.path.splitext(inputfile)
        if numbermodelruns == 1:
            outputfile = inputfileparts[0] + '.out'
        else:
            outputfile = inputfileparts[0] + str(modelrun) + '.out'
        sys.stdout.write('\nOutput to file: {}\n'.format(outputfile))
        sys.stdout.flush()
        f = prepare_output_file(outputfile, G)

        # Adjust position of sources and receivers if required
        if G.srcstepx > 0 or G.srcstepy > 0 or G.srcstepz > 0:
            for source in itertools.chain(G.hertziandipoles, G.magneticdipoles, G.voltagesources):
                source.positionx += (modelrun - 1) * G.srcstepx
                source.positiony += (modelrun - 1) * G.srcstepy
                source.positionz += (modelrun - 1) * G.srcstepz
        if G.rxstepx > 0 or G.rxstepy > 0 or G.rxstepz > 0:
            for receiver in G.rxs:
                receiver.positionx += (modelrun - 1) * G.rxstepx
                receiver.positiony += (modelrun - 1) * G.rxstepy
                receiver.positionz += (modelrun - 1) * G.rxstepz

        ##################################
        #   Main FDTD calculation loop   #
        ##################################
        tsolvestart = perf_counter()
        # Absolute time
        abstime = 0

        for timestep in range(G.iterations):
            if timestep == 0:
                tstepstart = perf_counter()
            
            # Write field outputs to file
            write_output(f, timestep, G.Ex, G.Ey, G.Ez, G.Hx, G.Hy, G.Hz, G)
            
            # Write any snapshots to file
            if G.snapshots:
                for snapshot in G.snapshots:
                    if snapshot.time == timestep + 1:
                        snapshot.write_snapshot(G.Ex, G.Ey, G.Ez, G.Hx, G.Hy, G.Hz, G)

            # Update electric field components
            # If there are any dispersive materials do 1st part of dispersive update. It is split into two parts as it requires present and updated electric field values.
            if Material.maxpoles == 1:
                update_ex_dispersive_1pole_A(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Tx, G.Ex, G.Hy, G.Hz)
                update_ey_dispersive_1pole_A(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Ty, G.Ey, G.Hx, G.Hz)
                update_ez_dispersive_1pole_A(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Tz, G.Ez, G.Hx, G.Hy)
            elif Material.maxpoles > 1:
                update_ex_dispersive_multipole_A(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Tx, G.Ex, G.Hy, G.Hz)
                update_ey_dispersive_multipole_A(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Ty, G.Ey, G.Hx, G.Hz)
                update_ez_dispersive_multipole_A(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsE, G.updatecoeffsdispersive, G.ID, G.Tz, G.Ez, G.Hx, G.Hy)
            # Otherwise all materials are non-dispersive so do standard update
            else:
                update_ex(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.ID, G.Ex, G.Hy, G.Hz)
                update_ey(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.ID, G.Ey, G.Hx, G.Hz)
                update_ez(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsE, G.ID, G.Ez, G.Hx, G.Hy)

            # Update electric field components with the PML correction
            update_electric_pml(G)

            # Update electric field components from sources
            if G.voltagesources:
                for voltagesource in G.voltagesources:
                    voltagesource.update_electric(abstime, G.updatecoeffsE, G.ID, G.Ex, G.Ey, G.Ez, G)
            if G.transmissionlines:
                for transmissionline in G.transmissionlines:
                    transmissionline.update_electric(abstime, G.Ex, G.Ey, G.Ez, G)
            if G.hertziandipoles:   # Update any Hertzian dipole sources last
                for hertziandipole in G.hertziandipoles:
                    hertziandipole.update_electric(abstime, G.updatecoeffsE, G.ID, G.Ex, G.Ey, G.Ez, G)

            # If there are any dispersive materials do 2nd part of dispersive update. It is split into two parts as it requires present and updated electric field values. Therefore it can only be completely updated after the electric field has been updated by the PML and source updates.
            if Material.maxpoles == 1:
                update_ex_dispersive_1pole_B(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsdispersive, G.ID, G.Tx, G.Ex)
                update_ey_dispersive_1pole_B(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsdispersive, G.ID, G.Ty, G.Ey)
                update_ez_dispersive_1pole_B(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsdispersive, G.ID, G.Tz, G.Ez)
            elif Material.maxpoles > 1:
                update_ex_dispersive_multipole_B(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsdispersive, G.ID, G.Tx, G.Ex)
                update_ey_dispersive_multipole_B(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsdispersive, G.ID, G.Ty, G.Ey)
                update_ez_dispersive_multipole_B(G.nx, G.ny, G.nz, G.nthreads, Material.maxpoles, G.updatecoeffsdispersive, G.ID, G.Tz, G.Ez)

            # Increment absolute time value
            abstime += 0.5 * G.dt
            
            # Update magnetic field components
            update_hx(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsH, G.ID, G.Hx, G.Ey, G.Ez)
            update_hy(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsH, G.ID, G.Hy, G.Ex, G.Ez)
            update_hz(G.nx, G.ny, G.nz, G.nthreads, G.updatecoeffsH, G.ID, G.Hz, G.Ex, G.Ey)

            # Update magnetic field components with the PML correction
            update_magnetic_pml(G)

            # Update magnetic field components from sources
            if G.transmissionlines:
                for transmissionline in G.transmissionlines:
                    transmissionline.update_magnetic(abstime, G.Hx, G.Hy, G.Hz, G)
            if G.magneticdipoles:
                for magneticdipole in G.magneticdipoles:
                    magneticdipole.update_magnetic(abstime, G.updatecoeffsH, G.ID, G.Hx, G.Hy, G.Hz, G)
        
            # Increment absolute time value
            abstime += 0.5 * G.dt
        
            # Calculate time for two iterations, used to estimate overall runtime
            if timestep == 1:
                tstepend = perf_counter()
                runtime = datetime.timedelta(seconds=int((tstepend - tstepstart) / 2 * G.iterations))
                sys.stdout.write('Estimated runtime [HH:MM:SS]: {}\n'.format(runtime))
                sys.stdout.write('Solving for model run {} of {}...\n'.format(modelrun, numbermodelruns))
                sys.stdout.flush()
            elif timestep > 1:
                update_progress((timestep + 1) / G.iterations)
            
        # Close output file
        f.close()
        tsolveend = perf_counter()
        print('\n\nSolving took [HH:MM:SS]: {}'.format(datetime.timedelta(seconds=int(tsolveend - tsolvestart))))
        print('Peak memory (approx) used: {}'.format(human_size(p.memory_info().rss)))




