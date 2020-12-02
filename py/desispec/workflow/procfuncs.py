import os
import glob
import json
from astropy.io import fits
from astropy.table import Table, join
import numpy as np
# import numpy as np

import argparse
import re
import time, datetime
import psutil
from os import listdir
from collections import OrderedDict
import subprocess
import sys
from copy import deepcopy



from desispec.workflow.queue import get_resubmission_states, update_from_queue
from desispec.workflow.timing import what_night_is_it
from desispec.workflow.desi_proc_funcs import get_desi_proc_batch_file_pathname, create_desi_proc_batch_script, \
                                              get_desi_proc_batch_file_path
from desispec.workflow.utils import pathjoin
from desispec.workflow.tableio import write_table
from desiutil.log import get_logger

#################################################
############## Misc Functions ###################
#################################################
def night_to_starting_iid(night=None):
    """
    Creates an internal ID for a given night. The resulting integer is an 8 digit number.
    The digits are YYMMDDxxx where YY is the years since 2000, MM and DD are the month and day. xxx are 000,
    and are incremented for up to 1000 unique job ID's for a given night.

    Args:
        night, str or int. YYYYMMDD of the night to get the starting internal ID for.

    Returns:
        internal_id, int. 9 digit number consisting of YYMMDD000. YY is years after 2000, MMDD is month and day.
                          000 being the starting job number (0).
    """
    if night is None:
        night = what_night_is_it()
    night = int(night)
    internal_id = (night - 20000000) * 1000
    return internal_id



#################################################
############ Script Functions ###################
#################################################
def batch_script_name(prow):
    """
    Wrapper script that takes a processing table row (or dictionary with NIGHT, EXPID, JOBDESC, CAMWORD defined)
    and determines the script file pathname as defined by desi_proc's helper functions.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for 'NIGHT', 'EXPID', 'JOBDESC', and 'CAMWORD'.

    Returns:
        scriptfile, str. The complete pathname to the script file, as it is defined within the desi_proc ecosystem.
    """
    pathname = get_desi_proc_batch_file_pathname(night = prow['NIGHT'], exp=prow['EXPID'], \
                                             jobdesc=prow['JOBDESC'], cameras=prow['CAMWORD'])
    scriptfile =  pathname + '.slurm'
    return scriptfile

def create_and_submit(prow, queue='realtime', dry_run=False, joint=False):
    """
    Wrapper script that takes a processing table row and three modifier keywords, creates a submission script for the
    compute nodes, and then submits that script to the Slurm scheduler with appropriate dependencies.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for processing_table columns found in
                                 desispect.workflow.proctable.get_processing_table_column_defs()
        queue, str. The name of the NERSC Slurm queue to submit to. Default is the realtime queue.
        dry_run, bool. If true, this is a simulated run and the scripts will not be written or submitted. Output will
                       relevant for testing will be printed as though scripts are being submitted. Default is False.
        joint, bool. Whether this is a joint fitting job (the job involves multiple exposures) and therefore needs to be
                     run with desi_proc_joint_fit. Default is False.

    Returns:
        prow, Table.Row or dict. The same prow type and keywords as input except with modified values updated to reflect
                                 the change in job status after creating and submitting the job for processing.

    Note:
        This modifies the input. Though Table.Row objects are generally copied on modification, so the change to the
        input object in memory may or may not be changed. As of writing, a row from a table given to this function will
        not change during the execution of this function (but can be overwritten explicitly with the returned row if desired).
    """
    prow = create_batch_script(prow, queue=queue, dry_run=dry_run, joint=joint)
    prow = submit_batch_script(prow, dry_run=dry_run)
    return prow

def desi_proc_command(prow, queue=None):
    """
    Wrapper script that takes a processing table row (or dictionary with NIGHT, EXPID, OBSTYPE, JOBDESC, CAMWORD defined)
    and determines the proper command line call to process the data defined by the input row/dict.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for 'NIGHT', 'EXPID', 'JOBDESC', and 'CAMWORD'.
        queue, str. The name of the NERSC Slurm queue to submit to. Default is None (which leaves it to the desi_proc default).

    Returns:
        cmd, str. The proper command to be submitted to desi_proc to process the job defined by the prow values.
    """
    if prow is None:
        import pdb
        pdb.set_trace()
    cmd = 'desi_proc'
    cmd += ' --batch'
    cmd += ' --nosubmit'
    cmd += ' --traceshift'
    if queue is not None:
        cmd += f' -q {queue}'
    if prow['OBSTYPE'].lower() == 'science':
        if prow['JOBDESC'] == 'prestdstar':
            cmd += ' --nostdstarfit --nofluxcalib'
        elif prow['JOBDESC'] == 'poststdstar':
            cmd += ' --noprestdstarfit --nostdstarfit'
    specs = ','.join(str(prow['CAMWORD'])[1:])
    cmd += ' --cameras={} -n {} -e {}'.format(specs, prow['NIGHT'], prow['EXPID'][0])
    return cmd

def desi_proc_joint_fit_command(prow, queue=None):
    """
    Wrapper script that takes a processing table row (or dictionary with NIGHT, EXPID, OBSTYPE, CAMWORD defined)
    and determines the proper command line call to process the data defined by the input row/dict.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for 'NIGHT', 'EXPID', 'JOBDESC', and 'CAMWORD'.
        queue, str. The name of the NERSC Slurm queue to submit to. Default is None (which leaves it to the desi_proc default).

    Returns:
        cmd, str. The proper command to be submitted to desi_proc_joint_fit to process the job defined by the prow values.
    """
    cmd = 'desi_proc_joint_fit'
    cmd += ' --batch'
    cmd += ' --nosubmit'
    cmd += ' --traceshift'
    if queue is not None:
        cmd += f' -q {queue}'

    descriptor = prow['OBSTYPE'].lower()
        
    night = prow['NIGHT']
    specs = ','.join(str(prow['CAMWORD'])[1:])
    expids = prow['EXPID']
    expid_str = ','.join([str(eid) for eid in expids])

    cmd += f' --obstype {descriptor}'
    cmd += ' --cameras={} -n {} -e {}'.format(specs, night, expid_str)
    return cmd

def create_batch_script(prow, queue='realtime', dry_run=False, joint=False):
    """
    Wrapper script that takes a processing table row and three modifier keywords and creates a submission script for the
    compute nodes.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for processing_table columns found in
                                 desispect.workflow.proctable.get_processing_table_column_defs()
        queue, str. The name of the NERSC Slurm queue to submit to. Default is the realtime queue.
        dry_run, bool. If true, this is a simulated run and the scripts will not be written or submitted. Output will
                       relevant for testing will be printed as though scripts are being submitted. Default is False.
        joint, bool. Whether this is a joint fitting job (the job involves multiple exposures) and therefore needs to be
                     run with desi_proc_joint_fit. Default is False.

    Returns:
        prow, Table.Row or dict. The same prow type and keywords as input except with modified values updated values for
                                 scriptname.

    Note:
        This modifies the input. Though Table.Row objects are generally copied on modification, so the change to the
        input object in memory may or may not be changed. As of writing, a row from a table given to this function will
        not change during the execution of this function (but can be overwritten explicitly with the returned row if desired).
    """
    log = get_logger()
    if joint:
        cmd = desi_proc_joint_fit_command(prow, queue=queue)
    else:
        cmd = desi_proc_command(prow, queue=queue)

    #log.debug(cmd)

    scriptpathname = batch_script_name(prow)
    if dry_run:
        log.info("Output file would have been: {}".format(scriptpathname))
        log.info("Command to be run: {}".format(cmd.split()))
    else:
        log.info("Running: {}".format(cmd.split()))
        scriptpathname = create_desi_proc_batch_script(night=prow['NIGHT'], exp=prow['EXPID'], \
                                                       cameras=prow['CAMWORD'], jobdesc=prow['JOBDESC'], \
                                                       queue=queue, cmdline=cmd)
        log.info("Outfile is: ".format(scriptpathname))

    prow['SCRIPTNAME'] = os.path.basename(scriptpathname)
    return prow


def submit_batch_script(prow, dry_run=False, strictly_successful=False):
    """
    Wrapper script that takes a processing table row and three modifier keywords and submits the scripts to the Slurm
    scheduler.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for processing_table columns found in
                                 desispect.workflow.proctable.get_processing_table_column_defs()
        dry_run, bool. If true, this is a simulated run and the scripts will not be written or submitted. Output will
                       relevant for testing will be printed as though scripts are being submitted. Default is False.
        strictly_successful, bool. Whether all jobs require all inputs to have succeeded. For daily processing, this is
                                   less desirable because e.g. the sciences can run with SVN default calibrations rather
                                   than failing completely from failed calibrations. Default is False.

    Returns:
        prow, Table.Row or dict. The same prow type and keywords as input except with modified values updated values for
                                 scriptname.

    Note:
        This modifies the input. Though Table.Row objects are generally copied on modification, so the change to the
        input object in memory may or may not be changed. As of writing, a row from a table given to this function will
        not change during the execution of this function (but can be overwritten explicitly with the returned row if desired).
    """
    log = get_logger()
    jobname = batch_script_name(prow)
    dependencies = prow['LATEST_DEP_QID']
    dep_list, dep_str = '', ''
    if dependencies is not None:
        jobtype = prow['JOBDESC']
        if strictly_successful:
            depcond = 'afterok'
        elif jobtype in ['flat','nightlyflat','poststdstar']:
            depcond = 'afterok'
        else:
            ## if arc, psfnight, prestdstar, or stdstarfit, any inputs is fine
            ## (though psfnight and stdstarfit will require some inputs otherwise they'll go up in flames)
            depcond = 'afterany'

        dep_str = f'--dependency={depcond}:'

        if np.isscalar(dependencies):
            dep_list = str(dependencies).strip(' \t')
            if dep_list == '':
                dep_str = ''
            else:
                dep_str += dep_list
        else:
            if len(dependencies)>1:
                dep_list = ':'.join(np.array(dependencies).astype(str))
                dep_str += dep_list
            elif len(dependencies) == 1 and dependencies[0] not in [None, 0]:
                dep_str += str(dependencies[0])
            else:
                dep_str = ''

    ## True function will actually submit to SLURM
    if dry_run:
        current_qid = int(time.time() - 1.6e9)
    else:
        # script = f'{jobname}.slurm'
        # script_path = pathjoin(batchdir, script)
        batchdir = get_desi_proc_batch_file_path(night=prow['NIGHT'])
        script_path = pathjoin(batchdir, jobname)
        if dep_str == '':
            current_qid = subprocess.check_output(['sbatch', '--parsable', f'{script_path}'],
                                                  stderr=subprocess.STDOUT, text=True)
        else:
            current_qid = subprocess.check_output(['sbatch', '--parsable',f'{dep_str}',f'{script_path}'],
                                                  stderr=subprocess.STDOUT, text=True)
        current_qid = int(current_qid.strip(' \t\n'))

    log.info(f'Submitted {jobname}  with dependencies {dep_str}. Returned qid: {current_qid}')

    prow['LATEST_QID'] = current_qid
    prow['ALL_QIDS'] = np.append(prow['ALL_QIDS'],current_qid)
    prow['STATUS'] = 'SU'
    prow['SUBMIT_DATE'] = int(time.time())
    
    return prow


#############################################
##########   Row Manipulations   ############
#############################################
def define_and_assign_dependency(prow, arcjob, flatjob):
    """
    Given input processing row and possible arcjob (processing row for psfnight) and flatjob (processing row for
    nightlyflat), this defines the JOBDESC keyword and assigns the dependency appropriate for the job type of prow.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for 'OBSTYPE'. A row must have column names for
                                 'JOBDESC', 'INT_DEP_IDS', and 'LATEST_DEP_ID'.
        arcjob, Table.Row, dict, or NoneType. Processing row corresponding to psfnight for the night of the data in prow.
                                              This must contain keyword accessible values for 'INTID', and 'LATEST_QID'.
                                              If None, it assumes the dependency doesn't exist and no dependency is assigned.
        flatkpb, Table.Row, dict, or NoneType. Processing row corresponding to nightlyflat for the night of the data in prow.
                                               This must contain keyword accessible values for 'INTID', and 'LATEST_QID'.
                                               If None, it assumes the dependency doesn't exist and no dependency is assigned.

    Returns:
        prow, Table.Row or dict. The same prow type and keywords as input except with modified values updated values for
                                 'JOBDESC', 'INT_DEP_IDS'. and 'LATEST_DEP_ID'.

    Note:
        This modifies the input. Though Table.Row objects are generally copied on modification, so the change to the
        input object in memory may or may not be changed. As of writing, a row from a table given to this function will
        not change during the execution of this function (but can be overwritten explicitly with the returned row if desired).
    """
    prow['JOBDESC'] = prow['OBSTYPE']
    if prow['OBSTYPE'] in ['science', 'twiflat']:
        dependency = flatjob
        prow['JOBDESC'] = 'prestdstar'
    elif prow['OBSTYPE'] == 'flat':
        dependency = arcjob
    else:
        dependency = None

    prow = assign_dependency(prow, dependency)

    return prow


def assign_dependency(prow, dependency):
    """
    Given input processing row and possible arcjob (processing row for psfnight) and flatjob (processing row for
    nightlyflat), this defines the JOBDESC keyword and assigns the dependency appropriate for the job type of prow.

    Args:
        prow, Table.Row or dict. Must include keyword accessible definitions for 'OBSTYPE'. A row must have column names for
                                 'JOBDESC', 'INT_DEP_IDS', and 'LATEST_DEP_ID'.
        dependency, Table.Row, dict, or NoneType. Processing row corresponding to the required input for the job in prow.
                                              This must contain keyword accessible values for 'INTID', and 'LATEST_QID'.
                                              If None, it assumes the dependency doesn't exist and no dependency is assigned.

    Returns:
        prow, Table.Row or dict. The same prow type and keywords as input except with modified values updated values for
                                 'JOBDESC', 'INT_DEP_IDS'. and 'LATEST_DEP_ID'.

    Note:
        This modifies the input. Though Table.Row objects are generally copied on modification, so the change to the
        input object in memory may or may not be changed. As of writing, a row from a table given to this function will
        not change during the execution of this function (but can be overwritten explicitly with the returned row if desired).
    """
    if dependency is not None:
        if type(dependency) in [list, np.array]:
            ids, qids = [], []
            for curdep in dependency:
                ids.append(curdep['INTID'])
                qids.append(curdep['LATEST_QID'])
            prow['INT_DEP_IDS'] = np.array(ids)
            prow['LATEST_DEP_QID'] = np.array(qids)
        else:
            prow['INT_DEP_IDS'] = np.array([dependency['INTID']])
            prow['LATEST_DEP_QID'] = np.array([dependency['LATEST_QID']])
    return prow


def get_type_and_tile(erow):
    """
    Trivial function to return the OBSTYPE and the TILEID from an exposure table row

    Args:
        erow, Table.Row or dict. Must contain 'OBSTYPE' and 'TILEID' as keywords.

    Returns:
        tuple (str, str), corresponding to the OBSTYPE and TILEID values of the input erow.
    """
    return str(erow['OBSTYPE']).lower(), erow['TILEID']


#############################################
#########   Table manipulators   ############
#############################################
def parse_previous_tables(etable, ptable, night):
    """
    This takes in the exposure and processing tables and regenerates all the working memory variables needed for the
    daily processing script.

    Used by the daily processing to define most of its state-ful variables into working memory.
    If the processing table is empty, these are simply declared and returned for use.
    If the code had previously run and exited (or crashed), however, this will all the code to
    re-establish itself by redefining these values.

    Args:
        etable, Table, Exposure table of all exposures that have been dealt with thus far.
        ptable, Table, Processing table of all exposures that have been processed.
        night, str or int, the night the data was taken.

    Returns:
        arcs, list of Table.Row's, list of the individual arc jobs used for the psfnight (NOT all
                                   the arcs, if multiple sets existed)
        flats, list of Table.Row's, list of the individual flat jobs used for the nightlyflat (NOT
                                    all the flats, if multiple sets existed)
        sciences, list of Table.Row's, list of the most recent individual prestdstar science exposures
                                       (if currently processing that tile)
        arcjob, Table.Row or None, the psfnight job row if it exists. Otherwise None.
        flatjob, Table.Row or None, the nightlyflat job row if it exists. Otherwise None.
        curtype, None, the obstype of the current job being run. Always None as first new job will define this.
        lasttype, str or None, the obstype of the last individual exposure row to be processed.
        curtile, None, the tileid of the current job (if science). Otherwise None. Always None as first
                       new job will define this.
        lasttile, str or None, the tileid of the last job (if science). Otherwise None.
        internal_id, int, an internal identifier unique to each job. Increments with each new job. This
                          is the latest unassigned value.
        last_not_dither, bool, True if the last job was a science dither tile. Otherwise False.
    """
    arcs, flats, sciences = [], [], []
    arcjob, flatjob = None, None
    curtype,lasttype = None,None
    curtile,lasttile = None,None

    if len(ptable) > 0:
        prow = ptable[-1]
        internal_id = int(prow['INTID'])+1
        lasttype,lasttile = get_type_and_tile(ptable[-1])
        last_not_dither = (prow['OBSDESC'] != 'dither')
        jobtypes = ptable['JOBDESC']

        if 'psfnight' in jobtypes:
            arcjob = ptable[jobtypes=='psfnight'][0]
        elif lasttype == 'arc':
            arcs = []
            seqnum = 10
            for row in ptable[::-1]:
                erow = etable[etable['EXPID']==row['EXPID'][0]]
                if row['OBSTYPE'].lower() == 'arc' and int(erow['SEQNUM'])<seqnum:
                    arcs.append(row)
                    seqnum = int(erow['SEQNUM'])
                else:
                    break

        if 'nightlyflat' in jobtypes:
            flatjob = ptable[jobtypes=='nightlyflat'][0]
        elif lasttype == 'flat':
            flats = []
            for row in ptable[::-1]:
                erow = etable[etable['EXPID']==row['EXPID'][0]]
                if row['OBSTYPE'].lower() == 'flat' and int(erow['SEQTOT'])<5:
                    flats.append(row)
                else:
                    break

        if lasttype.lower() == 'science':
            for row in ptable[::-1]:
                if row['OBSTYPE'].lower() == 'science' and row['TILEID'] == lasttile and \
                   row['JOBDESC'] == 'prestdstar' and row['OBSDESC'] != 'dither':
                    sciences.append(row)
                else:
                    break
    else:
        internal_id = night_to_starting_iid(night)
        last_not_dither = True

    return arcs,flats,sciences, \
           arcjob, flatjob, \
           curtype, lasttype, \
           curtile, lasttile,\
           internal_id, last_not_dither


def update_and_recurvsively_submit(proc_table, submits=0, resubmission_states=None, start_time=None, end_time=None,
                                   ptab_name=None, dry_run=False):
    """
    Given an processing table, this loops over job rows and resubmits failed jobs (as defined by resubmission_states).
    Before submitting a job, it checks the dependencies for failures. If a dependency needs to be resubmitted, it recursively
    follows dependencies until it finds the first job without a failed dependency and resubmits that. Then resubmits the
    other jobs with the new Slurm jobID's for proper dependency coordination within Slurm.

    Args:
        proc_table, Table, the processing table with a row per job.
        submits, int, the number of submissions made to the queue. Used for saving files and in not overloading the scheduler.
        resubmission_states, list or array of strings, each element should be a capitalized string corresponding to a
                                                       possible Slurm scheduler state, where you wish for jobs with that
                                                       outcome to be resubmitted
        start_time, str, datetime string in the format understood by NERSC Slurm scheduler. This should defined the earliest
                       date and time that you expected to have a job run in the queue. Used to narrow the window of jobs
                       to request information on.
        end_time, str, datetime string in the format understood by NERSC Slurm scheduler. This should defined the latest
                       date and time that you expected to have a job run in the queue. Used to narrow the window of jobs
                       to request information on.
        ptab_name, str, the full pathname where the processing table should be saved.
        dry_run, bool, whether this is a simulated run or not. If True, jobs are not actually submitted but relevant
                       information is printed to help with testing.
    Returns:
        proc_table: Table, a table with the same rows as the input except that Slurm and jobid relevant columns have
                           been updated for those jobs that needed to be resubmitted.
        submits: int, the number of submissions made to the queue. This is incremented from the input submits, so it is
                      the number of submissions made from this function call plus the input submits value.

    Note:
        This modifies the inputs of both proc_table and submits and returns them.
    """
    if resubmission_states is None:
        resubmission_states = get_resubmission_states()
    proc_table = update_from_queue(proc_table, start_time=start_time, end_time=end_time)
    id_to_row_map = {row['INTID']: rown for rown, row in enumerate(proc_table)}
    for rown in range(len(proc_table)):
        if proc_table['STATUS'][rown] in resubmission_states:
            proc_table, submits = recursive_submit_failed(rown, proc_table, \
                                                                   submits, id_to_row_map, ptab_name,
                                                                   resubmission_states, dry_run)
    return proc_table, submits

def recursive_submit_failed(rown, proc_table, submits, id_to_row_map, ptab_name=None,
                            resubmission_states=None, dry_run=False):
    """
    Given a row of a processing table and the full processing table, this resubmits the given job.
    Before submitting a job, it checks the dependencies for failures in the processing table. If a dependency needs to
    be resubmitted, it recursively follows dependencies until it finds the first job without a failed dependency and
    resubmits that. Then resubmits the other jobs with the new Slurm jobID's for proper dependency coordination within Slurm.

    Args:
        rown, Table.Row, the row of the processing table that you want to resubmit.
        proc_table, Table, the processing table with a row per job.
        submits, int, the number of submissions made to the queue. Used for saving files and in not overloading the scheduler.
        id_to_row_map, dict, lookup dictionary where the keys are internal ids (INTID's) and the values are the row position
                             in the processing table.
        ptab_name, str, the full pathname where the processing table should be saved.
        resubmission_states, list or array of strings, each element should be a capitalized string corresponding to a
                                                       possible Slurm scheduler state, where you wish for jobs with that
                                                       outcome to be resubmitted
        dry_run, bool, whether this is a simulated run or not. If True, jobs are not actually submitted but relevant
                       information is printed to help with testing.
    Returns:
        proc_table: Table, a table with the same rows as the input except that Slurm and jobid relevant columns have
                           been updated for those jobs that needed to be resubmitted.
        submits: int, the number of submissions made to the queue. This is incremented from the input submits, so it is
                      the number of submissions made from this function call plus the input submits value.

    Note:
        This modifies the inputs of both proc_table and submits and returns them.
    """
    log = get_logger()
    if resubmission_states is None:
        resubmission_states = get_resubmission_states()
    ideps = proc_table['INT_DEP_IDS'][rown]
    if ideps is None:
        proc_table['LATEST_DEP_QID'][rown] = None
    else:
        qdeps = []
        for idep in np.sort(np.atleast_1d(ideps)):
            if proc_table['STATUS'][id_to_row_map[idep]] in resubmission_states:
                proc_table, submits = recursive_submit_failed(id_to_row_map[idep], \
                                                              proc_table, submits, id_to_row_map)
            qdeps.append(proc_table['LATEST_QID'][id_to_row_map[idep]])

        qdeps = np.atleast_1d(qdeps)
        if len(qdeps) > 0:
            proc_table['LATEST_DEP_QID'][rown] = qdeps
        else:
            log.info("Error: number of qdeps should be 1 or more")
            log.info(f'Rown {rown}, ideps {ideps}')

    proc_table[rown] = submit_batch_script(proc_table[rown], dry_run=dry_run)
    submits += 1

    if dry_run:
        pass
    else:
        time.sleep(2)
        if submits % 10 == 0:
            if ptab_name is None:
                write_table(proc_table, tabletype='processing', overwrite=True)
            else:
                write_table(proc_table, tablename=ptab_name, overwrite=True)
            time.sleep(60)
        if submits % 100 == 0:
            time.sleep(540)
            proc_table = update_from_queue(proc_table)
            if ptab_name is None:
                write_table(proc_table, tabletype='processing', overwrite=True)
            else:
                write_table(proc_table, tablename=ptab_name, overwrite=True)

    return proc_table, submits


#########################################
########     Joint fit     ##############
#########################################
def joint_fit(ptable, prows, internal_id, queue, descriptor, dry_run=False):
    """
    Given a set of prows, this generates a processing table row, creates a batch script, and submits the appropriate
    joint fitting job given by descriptor. If the joint fitting job is standard star fitting, the post standard star fits
    for all the individual exposures also created and submitted. The returned ptable has all of these rows added to the
    table given as input.

    Args:
        ptable, Table. The processing table where each row is a processed job.
        prows, list or array of Table.Rows or dicts. The rows corresponding to the individual exposure jobs that are
                                                     inputs to the joint fit.
        internal_id, int, the next internal id to be used for assignment (already incremented up from the last used id number used).
        queue, str. The name of the queue to submit the jobs to. If None is given the current desi_proc default is used.
        descriptor, str. Description of the joint fitting job. Can either be 'science' or 'stdstarfit', 'arc' or 'psfnight',
                         or 'flat' or 'nightlyflat'.
        dry_run, bool, whether this is a simulated run or not. If True, jobs are not actually submitted but relevant
                       information is printed to help with testing.

    Returns:
        ptable, Table. The same processing table as input except with added rows for the joint fit job and, in the case
                       of a stdstarfit, the poststdstar science exposure jobs.
        joint_prow, Table.Row or dict. Row of a processing table corresponding to the joint fit job.
    """
    if descriptor is None:
        return ptable, None
    elif descriptor == 'science':
        descriptor = 'stdstarfit'
    elif descriptor == 'arc':
        descriptor = 'psfnight'
    elif descriptor == 'flat':
        descriptor = 'nightlyflat'

    if descriptor not in ['stdstarfit', 'psfnight', 'nightlyflat']:
        return ptable, None

    joint_prow = make_joint_prow(prows, descriptor=descriptor, internal_id=internal_id)
    joint_prow = create_and_submit(joint_prow, queue=queue, joint=True, dry_run=dry_run)
    ptable.add_row(joint_prow)

    if descriptor == 'stdstarfit':
        for row in prows:
            row['JOBDESC'] = 'poststdstar'
            row['INTID'] = internal_id
            row['ALL_QIDS'] = np.ndarray(shape=0).astype(int)
            internal_id += 1
            row = assign_dependency(row, joint_prow)
            row = create_and_submit(row, dry_run=dry_run)
            ptable.add_row(row)
    else:
        ptable = set_calibrator_flag(prows, ptable)

    return ptable, joint_prow


## wrapper functions for joint fitting
def science_joint_fit(ptable, sciences, internal_id, queue='realtime', dry_run=False):
    """
    Wrapper function for desiproc.workflow.procfuns.joint_fit specific to the stdstarfit joint fit.

    All variables are the same except:
        Arg 'sciences' is mapped to the prows argument of joint_fit.
        The joint_fit argument descriptor is pre-defined as 'stdstarfit'.
    """
    return joint_fit(ptable=ptable, prows=sciences, internal_id=internal_id, queue=queue, descriptor='stdstarfit',
                     dry_run=dry_run)


def flat_joint_fit(ptable, flats, internal_id, queue='realtime', dry_run=False):
    """
    Wrapper function for desiproc.workflow.procfuns.joint_fit specific to the nightlyflat joint fit.

    All variables are the same except:
        Arg 'flats' is mapped to the prows argument of joint_fit.
        The joint_fit argument descriptor is pre-defined as 'nightlyflat'.
    """
    return joint_fit(ptable=ptable, prows=flats, internal_id=internal_id, queue=queue, descriptor='nightlyflat',
                     dry_run=dry_run)


def arc_joint_fit(ptable, arcs, internal_id, queue='realtime', dry_run=False):
    """
    Wrapper function for desiproc.workflow.procfuns.joint_fit specific to the psfnight joint fit.

    All variables are the same except:
        Arg 'arcs' is mapped to the prows argument of joint_fit.
        The joint_fit argument descriptor is pre-defined as 'psfnight'.
    """
    return joint_fit(ptable=ptable, prows=arcs, internal_id=internal_id, queue=queue, descriptor='psfnight',
                     dry_run=dry_run)


def make_joint_prow(prows, descriptor, internal_id):
    """
    Given an input list or array of processing table rows and a descriptor, this creates a joint fit processing job row.
    It starts by copying the first input row, overwrites relevant columns, and defines the new dependencies (based on the
    input prows).

    Args:
        prows, list or array of Table.Rows or dicts. The rows corresponding to the individual exposure jobs that are
                                                     inputs to the joint fit.
        descriptor, str. Description of the joint fitting job. Can either be 'stdstarfit', 'psfnight', or 'nightlyflat'.
        internal_id, int, the next internal id to be used for assignment (already incremented up from the last used id number used).

    Returns:
        joint_prow, Table.Row or dict. Row of a processing table corresponding to the joint fit job.
    """
    if type(prows[0]) in [dict, OrderedDict]:
        joint_prow = prows[0].copy()
    else:
        joint_prow = OrderedDict()
        for nam in prows[0].colnames:
            joint_prow[nam] = prows[0][nam]

    joint_prow['INTID'] = internal_id
    joint_prow['JOBDESC'] = descriptor
    joint_prow['ALL_QIDS'] = np.ndarray(shape=0).astype(int)
    if type(prows) in [list, np.array]:
        ids, qids, expids = [], [], []
        for currow in prows:
            ids.append(currow['INTID'])
            qids.append(currow['LATEST_QID'])
            expids.append(currow['EXPID'][0])
        joint_prow['INT_DEP_IDS'] = np.array(ids)
        joint_prow['LATEST_DEP_QID'] = np.array(qids)
        joint_prow['EXPID'] = np.array(expids)
    else:
        joint_prow['INT_DEP_IDS'] = np.array([prows['INTID']])
        joint_prow['LATEST_DEP_QID'] = np.array([prows['LATEST_QID']])
        joint_prow['EXPID'] = prows['EXPID']

    return joint_prow

def checkfor_and_submit_joint_job(ptable, arcs, flats, sciences, arcjob, flatjob, \
                                  lasttype, last_not_dither, internal_id, dry_run=False, queue='realtime'):
    """
    Takes all the state-ful data from daily processing and determines whether a joint fit needs to be submitted. Places
    the decision criteria into a single function for easier maintainability over time. These are separate from the
    new standard manifest*.json method of indicating a calibration sequence is complete. That is checked independently
    elsewhere and doesn't interact with this.

    Args:
        ptable, Table, Processing table of all exposures that have been processed.
        arcs, list of Table.Row's, list of the individual arc jobs to be used for the psfnight (NOT all
                                   the arcs, if multiple sets existed). May be empty if none identified yet.
        flats, list of Table.Row's, list of the individual flat jobs to be used for the nightlyflat (NOT
                                    all the flats, if multiple sets existed). May be empty if none identified yet.
        sciences, list of Table.Row's, list of the most recent individual prestdstar science exposures
                                       (if currently processing that tile). May be empty if none identified yet.
        arcjob, Table.Row or None, the psfnight job row if it exists. Otherwise None.
        flatjob, Table.Row or None, the nightlyflat job row if it exists. Otherwise None.
        lasttype, str or None, the obstype of the last individual exposure row to be processed.
        last_not_dither, bool, True if the last job was a science dither tile. Otherwise False.
        internal_id, int, an internal identifier unique to each job. Increments with each new job. This
                          is the smallest unassigned value.
        dry_run, bool, whether this is a simulated run or not. If True, jobs are not actually submitted but relevant
                       information is printed to help with testing.
        queue, str. The name of the queue to submit the jobs to. If None is given the current desi_proc default is used.

    Returns:
        ptable, Table, Processing table of all exposures that have been processed.
        arcjob, Table.Row or None, the psfnight job row if it exists. Otherwise None.
        flatjob, Table.Row or None, the nightlyflat job row if it exists. Otherwise None.
        internal_id, int, if no job is submitted, this is the same as the input, otherwise it is incremented upward from
                          from the input such that it represents the smallest unused ID.
    """
    if lasttype == 'science' and last_not_dither:
        ptable, tilejob = science_joint_fit(ptable, sciences, internal_id, dry_run=dry_run, queue=queue)
        internal_id += 1
    elif lasttype == 'flat' and flatjob is None and len(flats) > 10:
        ptable, flatjob = flat_joint_fit(ptable, flats, internal_id, dry_run=dry_run, queue=queue)
        internal_id += 1
    elif lasttype == 'arc' and arcjob is None and len(arcs) > 4:
        ptable, arcjob = arc_joint_fit(ptable, arcs, internal_id, dry_run=dry_run, queue=queue)
        internal_id += 1
    return ptable, arcjob, flatjob, internal_id


def set_calibrator_flag(prows, ptable):
    """
    Sets the "CALIBRATOR" column of a procesing table row to 1 (integer representation of True)
     for all input rows. Used within joint fitting code to flag the exposures that were input
     to the psfnight or nightlyflat for later reference.

    Args:
        prows, list or array of Table.Rows or dicts. The rows corresponding to the individual exposure jobs that are
                                                     inputs to the joint fit.
        ptable, Table. The processing table where each row is a processed job.

    Returns:
        ptable, Table. The same processing table as input except with added rows for the joint fit job and, in the case
                       of a stdstarfit, the poststdstar science exposure jobs.
    """
    for prow in prows:
        ptable['CALIBRATOR'][ptable['INTID'] == prow['INTID']] = 1
    return ptable

