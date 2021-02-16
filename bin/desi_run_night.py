#!/usr/bin/env python
# coding: utf-8

import argparse

from desispec.scripts.submit_night import submit_night

def parse_args():  # options=None):
    """
    Creates an arguments parser for the desi run production
    """
    parser = argparse.ArgumentParser(description="Submit a full production run of the DESI data pipeline for processing.")

    parser.add_argument("-n","--night", type=str, required=True, help="The night you want processed.")
    parser.add_argument("--proc-obstypes", type=str, default=None, required=False,
                        help="The basic data obstypes to submit for processing. " +
                             "E.g. science, dark, twilight, flat, arc, zero.")
    # File and dir defs
    parser.add_argument("-s", "--specprod", type=str, required=False, default=None,
                        help="Subdirectory under DESI_SPECTRO_REDUX to write the output files. "+\
                             "Overwrites the environment variable SPECPROD")
    parser.add_argument("-q", "--queue", type=str, required=False, default='realtime',
                        help="The queue to submit jobs to. Default is realtime.")
    parser.add_argument("--exp-table-path", type=str, required=False, default=None,
                        help="Directory name where the output exposure table should be saved.")
    parser.add_argument("--proc-table-path", type=str, required=False, default=None,
                        help="Directory name where the output processing table should be saved.")
    parser.add_argument("--table-file-type", type=str, required=False, default='csv',
                        help="File format and extension for the exp and proc tables.")
    # Code Flags
    parser.add_argument("--dry-run", action="store_true",
                        help="Perform a dry run where no jobs are actually created or submitted.")
    parser.add_argument("--error-if-not-available", action="store_true",
                        help="Raise an error instead of reporting and moving on if an exposure "+\
                             "table doesn't exist.")

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = parse_args()

    submit_night(**args.__dict__)