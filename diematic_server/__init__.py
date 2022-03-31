"""
Package definition for the diematic boiler.
We are exposing only the Boiler class. Note that, in order to
create an instance of the Boiler class it is required to read
the configuration file in yaml format that will provide the
registers available in your boiler. There is more information
and examples in the diematicd.py file
"""
from boiler import Boiler