#!/bin/bash

venv_path="venv"
source "$venv_path/bin/activate"
python3 main.py
deactivate
