#!/usr/bin/env python
# coding: utf-8

import argparse
import socket
import sys

from desispec.scripts.submit_prod import submit_production

def parse_args():  # options=None):
    """
    Creates an arguments parser for the desi run production
    """
    parser = argparse.ArgumentParser(description="Submit a full production run of the DESI data pipeline for processing.")

    parser.add_argument("--production-yaml", type=str, required=True,
                        help="Relative or absolute pathname to the yaml file summarizing the production.")

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

    submit_production(**args.__dict__)
