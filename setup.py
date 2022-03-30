"""Setup for the pipy package"""
from distutils.core import setup
setup(
  name = 'diematic-server',
  packages = ['diematic-server'],
  version = '1.0',
  license='MIT',
  description = 'Unix daemon and supporting models for publshing data from Diematic DeDietrich boiler',
  author = 'Ignacio Hernández-Ros',
  author_email = 'ignacio@hernandez-ros.com',
  url = 'https://github.com/IgnacioHR/diematic-server',
  download_url = 'https://github.com/IgnacioHR/diematic-server/archive/refs/tags/1.0.tar.gz',    # I explain this later on
  keywords = ['python', 'home-automation', 'iot', 'influxdb', 'restful', 'modbus', 'de-dietrich', 'diematic'],
  install_requires=[
          'certifi',
          'chardet',
					'daemon',
					'docutils',
					'idna',
					'influxdb',
					'lockfile',
					'pkg-resources',
					'pymodbus',
					'pyserial',
					'python-daemon',
					'python-dateutil',
					'pytz',
					'PyYAML',
					'requests',
					'six',
					'urllib3'
      ],
  classifiers=[
    'Development Status :: 5 - Production/Stable',
    'Intended Audience :: Developers',
    'Topic :: Home automation :: IOT',
    'License :: MIT License',
    'Programming Language :: Python :: 3.7',
  ],
)