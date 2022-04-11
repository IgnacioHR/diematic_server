""" 
We will use aiohttp for the new web server. 
This shall provide http 1.1 support and hopefully shall not produce 
aiohttp.client_exceptions.ClientPayloadError: Response payload is not completed
problems on the client side. Let's see how it works!
"""

import json
from aiohttp import web

# def _parameter_names(boiler) -> list:
# 	parameter_names = []
# 	for register in boiler.index:
# 		if register['type'] == 'bits':
# 			for bit in register['bits']:
# 				if bit != "io_unused":
# 					parameter_names.append(bit)
# 		else:
# 			parameter_names.append(register['name'])
# 	parameter_names.sort()
# 	return parameter_names


class DiematicWebRequestHandler:
	""" 
		This class implements the web server that provides GET and POST
		requests for the parameters of the boiler

		The parameters are defined in the same diematic.yaml file

		URL format:
		GET http://{host:port}/diematic/parameters
		returns a list of known parameters from the diematic.yaml

		GET http://{host:port}/diematic/parameters/{parameterName}
		return a JSON 
		{
			"name": "parameterName",
			"status": "read",
			"value": 34,
			"id": 680,
			"influx": true,
			"read": "2022-04-02T17:21:32.751479"
		}

		name: is the parameter name
		status: can be:
			"init": the value has not been read, the record is initialized
			"read": the value has been read
			"writepending": there is a new value pending to be written
			"checking": the value has been written, the boiler is pending reading to check if the new value has been successfully set
			"error": a problem occurred while setting the value
		value: the parameter value
		read: the last time the value was set
		newvalue: when status is "writepending" this record holds the value to be written
		error: the error message when status is "error"

		POST http://{host}/diematic/parameters/{parameterName}
		body must contain a json
		{
			"parameterName": value
		}
	"""

	routes = web.RouteTableDef()
	parameter_names = []

	def __init__(self, boiler) -> None:
		DiematicWebRequestHandler.parameter_names.clear()
		for register in boiler.index:
			if 'type' in list(register) and register['type'] == 'bits':
				for bit in register['bits']:
					if bit != "io_unused":
						DiematicWebRequestHandler.parameter_names.append(bit)
			elif 'name' in list(register):
				DiematicWebRequestHandler.parameter_names.append(register['name'])
		DiematicWebRequestHandler.parameter_names.sort()

	@routes.get('/diematic/parameters')
	async def send_list(request):
		""" produces a list of well known register names."""
		scheme = request.scheme
		host = request.host
		document = f"""
<html>
	<head>
		<title>Diematic REST controller by IHR at home (Ignacio Hernández-Ros)</title>
		<style type="text/css">
			body {{
				font-size: 0.9em;
				font-family: sans-serif;
			}}
			.styled-table {{
				border-collapse: collapse;
				margin: 25px 0;
				min-width: 400px;
				box-shadow: 0 0 20px rgba(0, 0, 0, 0.15);
			}}
			.styled-table thead tr {{
				background-color: #009879;
				color: #ffffff;
				text-align: left;
			}}
			.styled-table th,
			.styled-table td {{
					padding: 12px 15px;
			}}
			.styled-table tbody tr {{
					border-bottom: 1px solid #dddddd;
			}}
			.styled-table tbody tr:nth-of-type(even) {{
					background-color: #f3f3f3;
			}}
			.styled-table tbody tr:last-of-type {{
					border-bottom: 2px solid #009879;
			}}
		</style>
		<script>
			function changeValue(parameter) {{
				let newValue = prompt('Change value for '+parameter+' parameter', '');
				if (newValue) {{
					let xhr = new XMLHttpRequest();
					let url = '/diematic/parameters/'+parameter;

					xhr.open("POST", url, true);
					xhr.setRequestHeader("Content-Type", "application/json");
					xhr.onreadystatechange = function () {{
							if (xhr.readyState === 4 && xhr.status === 200) {{
									window.alert('The value is set\\nreview write progress reading the parameter!');
							}}
          }};
					var data = '{{ "value": '+newValue+' }}';
 					xhr.send(data);
				}}
			}}
			function resumeSetValue(parameter) {{
				let xhr = new XMLHttpRequest();
				let url = '/diematic/parameters/'+parameter+'/resume';

				xhr.open("POST", url, true);
				xhr.setRequestHeader("Content-Type", "application/json");
				xhr.onreadystatechange = function () {{
						if (xhr.readyState === 4 && xhr.status === 200) {{
								window.alert('write operation resumed!');
						}}
				}};
				xhr.send();
			}}
		</script>
	</head>
<body>
	<table class="styled-table">
		<thead>
			<tr><th colspan="3">Usage:</th></tr>
		</thead>
		<tbody>
			<tr><td>GET</td><td>{scheme}://{host}/diematic/parameters</td><td><b>returns this page</b></td></tr>
			<tr><td>GET</td><td>{scheme}://{host}/diematic/parameter/{{name}}</td><td><b>returns json with parameter information</b></td></tr>
			<tr><td>GET</td><td>{scheme}://{host}/diematic/config</td><td><b>returns boiler configuration parameters</b></td></tr>
			<tr><td>GET</td><td>{scheme}://{host}/diematic/json</td><td><b>returns all boiler parameters in single json</b></td></tr>
			<tr><td>POST</td><td>{scheme}://{host}/diematic/parameter/{{name}}</td><td><b>Set a parameter value. The body must be a json of this shape {{"value": XX}}. After the POST, the parameters may take some time to be written to the boiler. Use GET with the parameter name for information about the write operation status.</b></td></tr>
			<tr><td>POST</td><td>{scheme}://{host}/diematic/parameter/{{name}}/resume</td><td><b>If, for any reason, a write operation fails, a post like this will reset parameter to normal status.</b></td></tr>
		</tbody>
	</table>
	<p>Recognized parameters list</p>
	<ul>"""
		for name in DiematicWebRequestHandler.parameter_names:
			document = document + f"<li><a href='/diematic/parameters/{name}'>{name}</a>&nbsp;<button type=\"button\" onclick=\"changeValue(\'{name}\')\">change</button>&nbsp;<button type=\"button\" onclick=\"resumeSetValue(\'{name}\')\">Resume</button></li>\n"
		document = document + """</ul></body></html>"""
		return web.Response(text=document, content_type='text/html')

	@routes.get('/diematic/parameters/{paramName}')
	async def send_param(request):
		param_name = request.match_info.get('paramName')
		if not param_name in DiematicWebRequestHandler.parameter_names:
			return web.Response(status=422, reason=f'\'{param_name}\' is an invalid parameter')
		boiler = request.app["mainapp"].MyBoiler
		value = getattr(boiler, param_name)
		return web.json_response(value)

	@routes.post('/diematic/parameters/{paramName}')
	async def set_param(request):
		param_name = request.match_info.get('paramName')
		if not param_name in DiematicWebRequestHandler.parameter_names:
			return web.Response(status=422, reason=f'\'{param_name}\' is an invalid parameter')
		content_len = request.content_length
		if not content_len is None:
			data = await request.content.read(content_len)
		else:
			data = await request.content.read()
		jsoninput = json.loads(data.decode('utf8'))
		value = jsoninput['value']
		mainapp = request.app["mainapp"]
		mainapp.MyBoiler.set_write_pending(param_name, value)
		mainapp.check_pending_writes()
		return web.Response()

	@routes.post('/diematic/parameters/{paramName}/resume')
	async def set_param(request):
		param_name = request.match_info.get('paramName')
		if not param_name in DiematicWebRequestHandler.parameter_names:
			return web.Response(status=422, reason=f'\'{param_name}\' is an invalid parameter')
		mainapp = request.app["mainapp"]
		mainapp.MyBoiler.clear_error(param_name)
		return web.Response()

	@routes.get('/diematic/json')
	async def send_json(request):
		config = request.app["mainapp"].MyBoiler.toJSON()
		return web.json_response(config)

	@routes.get('/diematic/config')
	async def send_config(request):
		config = request.app["mainapp"].toJSON()
		return web.json_response(config)