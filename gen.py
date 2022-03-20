"""
Application to help in the generation of diematic.yaml file.
This small application was created to help in the generation of
the diematic.yaml file by generating in a bunch all registers
related with activation time of boiler circuits.

Use the records in the circuits array to decide what to generate. In my case
I'm using only circuit 'a' and 'acs' so I don't need to activate the others
this saves the number of records in the diematic.yaml file and the number
of records to he read and processed on every loop
"""
import yaml

days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

circuits = [
	{
		'name': 'a',
		'register': 126, # 146
		'exec': True, # change to true to generate the set of registers for circuit 'a'
	},
	{
		'name': 'b',
		'register': 147, # 167
		'exec': False,
	},
	{
		'name': 'c',
		'register': 168, # 188
		'exec': False,
	},
	{
		'name': 'acs',
		'register': 189, # 209
		'exec': True,
	},
	{
		'name': 'extra',
		'register': 210, # 230
		'exec': False,
	},
]
data = []

for circuit in circuits:
	if not circuit['exec']:
		continue
	register = circuit['register']
	cname = circuit['name']
	for day in days:
		line=0
		newline=True
		for starthour in range(0,24):
			for startminute in ['00', '30']:
				if newline:
					# hr = []
					entry = {
						'id': register,
						'type': 'bits',
						'bits': [],
					}
					register += 1
					data.append(entry)
					newline = False

				endhour = starthour if startminute == '00' else starthour + 1
				if endhour == 24:
					endhour = 0
				
				endminute = '30' if startminute == '00' else '00'
				varname = f"{day}_{cname}_{starthour:02}{startminute}_{endhour:02}{endminute}"
				data[len(data)-1]['bits'].append(varname)
				line += 1
				if line == 16:
					line = 0
					newline=True
					data[len(data)-1]['bits'].reverse()

datas = yaml.safe_dump(data, sort_keys=False)
print(datas)