# coding=UTF-8
"""Setup for the pipy package"""
import setuptools

with open('README.md', 'r', encoding='utf-8') as long_description_f:
	long_description = long_description_f.read()

setuptools.setup(
  name = 'diematic_server',
  version = '1.1',
  description = 'Unix daemon and supporting models for publishing data from Diematic DeDietrich boiler',
	long_description = long_description,
	long_description_content_type = 'text/markdown; charset=UTF-8',
  author = 'Ignacio Hernández-Ros',
  author_email = 'ignacio@hernandez-ros.com',
  packages = ['diematic_server'],
  license='MIT',
  url = 'https://github.com/IgnacioHR/diematic_server',
  download_url = 'https://github.com/IgnacioHR/diematic_server/archive/refs/tags/v1.2.tar.gz',
  keywords = ['python', 'home-automation', 'iot', 'influxdb', 'restful', 'modbus', 'de-dietrich', 'diematic'],
  install_requires=[
          'certifi',
          'chardet',
					'daemon',
					'docutils',
					'idna',
					'influxdb',
					'lockfile',
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
		'Environment :: No Input/Output (Daemon)',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: MIT License',
		'Operating System :: Unix',
    'Programming Language :: Python :: 3.7',
		'Topic :: Home Automation',
	],
)