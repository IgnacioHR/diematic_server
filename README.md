# diematic

A Unix service written in Python to monitor De Dietrich boiler equiped with Diematic system using Modbus RS-845 protocol.
The service reads data from the boiler and makes it available to be consumed in two ways:
Optionally, the values fetched from the boiler are sent to an InfluxDB database.
Optionally, a RESTful web server is installed and values can be obtained using GET and modified using POST requests.

![Screenshot](images/web-requests.png?raw=true)
![Screenshot](images/chronograf_screenshot.png?raw=true)

## Hardware requirements

 * A De Dietrich boiler with Diematic regulation and a mini-din socket
 * A mini-din cable 
 * A RS-845 to USB adapter
 * A nano-computer with a USB port and Python3 installed (Raspberry pi or similar)

Check tutorials in the "references" section below on how to do the hardware setup.

## Installation
```
git clone https://github.com/IgnacioHR/diematicd.git
cd diematicd
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir /etc/diematic
cp diematic.yaml /etc/diematic
cp diematicd.service /etc/systemd/system
systemctl enable diematicd
systemctl start diematicd
```

## Test
Run `python3 diematicd.py --help`
```
usage: diematicd.py [-h] [-b {none,influxdb}] [-d DEVICE] [-f]
                    [-l {critical,error,warning,info,debug}] [-c CONFIG]
                    [-w HOSTNAME] [-p PORT] [-s {loop,web,both}] [-a ADDRESS]
                    [-t {Raw,DiematicOneDecimal,DiematicModeFlag,ErrorCode,DiematicCircType,DiematicProgram,bit0,bit1,bit2,bit3,bit4,bit5,bit6,bit7,bit8,bit9,bitA,bitB,bitC,bitD,bitE,bitF}]
                    {status,start,stop,restart,reload,runonce,readregister}

positional arguments:
  {status,start,stop,restart,reload,runonce,readregister}
                        action to take

optional arguments:
  -h, --help            show this help message and exit
  -b {none,influxdb}, --backend {none,influxdb}
                        select data backend (default is influxdb)
  -d DEVICE, --device DEVICE
                        define modbus device
  -f, --foreground      Run in the foreground do not detach process
  -l {critical,error,warning,info,debug}, --logging {critical,error,warning,info,debug}
                        define logging level (default is critical)
  -c CONFIG, --config CONFIG
                        alternate configuration file
  -w HOSTNAME, --hostname HOSTNAME
                        web server host name, defaults to 0.0.0.0
  -p PORT, --port PORT  web server port, defaults to 8080
  -s {loop,web,both}, --server {loop,web,both}
                        servers to start
  -a ADDRESS, --address ADDRESS
                        register address to read whe action is readregister
  -t {Raw,DiematicOneDecimal,DiematicModeFlag,ErrorCode,DiematicCircType,DiematicProgram,bit0,bit1,bit2,bit3,bit4,bit5,bit6,bit7,bit8,bit9,bitA,bitB,bitC,bitD,bitE,bitF}, --format {Raw,DiematicOneDecimal,DiematicModeFlag,ErrorCode,DiematicCircType,DiematicProgram,bit0,bit1,bit2,bit3,bit4,bit5,bit6,bit7,bit8,bit9,bitA,bitB,bitC,bitD,bitE,bitF}
                        value format to apply for register read, default is
                        Raw

```

## InfluxDB preparation
### Minimal
```
CREATE DATABASE "diematic"
CREATE USER "diematic" WITH PASSWORD 'mySecurePas$w0rd'
GRANT ALL ON "diematic" TO "diematic"
CREATE RETENTION POLICY "one_week" ON "diematic" DURATION 1w REPLICATION 1 DEFAULT
```

### Additionnal steps for down-sampling
```
CREATE RETENTION POLICY "five_weeks" ON "diematic" DURATION 5w REPLICATION 1
CREATE RETENTION POLICY "five_years" ON "diematic" DURATION 260w REPLICATION 1

CREATE CONTINUOUS QUERY "cq_month" ON "diematic" BEGIN
  SELECT mean(/temperature/) AS "mean_1h", mean(/pressure/) AS "mean_1h", max(/temperature/) AS "max_1h", max(/pressure/) AS "max_1h"
  INTO "five_weeks".:MEASUREMENT
  FROM "one_week"."diematic"
  GROUP BY time(1h),*
END

CREATE CONTINUOUS QUERY "cq_year" ON "diematic" BEGIN
  SELECT mean(/^mean_.*temperature/) AS "mean_24h", mean(/^mean_.*pressure/) AS "mean_24h", max(/^max_.*temperature/) AS "max_24h", max(/^max_.*pressure/) AS "max_24h"
  INTO "five_years".:MEASUREMENT
  FROM "five_weeks"."diematic"
  GROUP BY time(24h),*
END
```


## References
- https://github.com/gmasse/diematic.git
- https://github.com/riptideio/pymodbus
- (french) http://sarakha63-domotique.fr/chaudiere-de-dietrich-domotise-modbus/amp/
- (french) https://www.dom-ip.com/wiki/Réalisation_d%27une_Interface_Web_pour_une_chaudière_De_Dietrich_équipée_d%27une_régulation_Diematic_3
- (french forum) https://www.domotique-fibaro.fr/topic/5677-de-dietrich-diematic-isystem/
- ~~(french forum) http://www.wit-square.fr/forum/topics/de-dietrich-communication-modbus-bi-ma-tre~~
- (french, modbus registers sheets, copy from previous forum) https://drive.google.com/file/d/156qBsfRGJvOpJBJu5K4WMHUuwv34bZQN/view?usp=sharing
