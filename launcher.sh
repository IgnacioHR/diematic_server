#!/bin/bash

WORKDIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
cd $WORKDIR

. ./venv/bin/activate
python diematic_server/diematic.py start
