from http.server import BaseHTTPRequestHandler
import json

def _parameter_names(boiler):
	parameter_names = []
	for register in boiler.index:
		if register['type'] == 'bits':
			for bit in register['bits']:
				if bit != "io_unused":
					parameter_names.append(bit)
		else:
			parameter_names.append(register['name'])
	parameter_names.sort()
	return parameter_names

def MakeDiematicWebRequestHandler(param):
	class DiematicWebRequestHandler(DiematicLocalWebRequestHandler):
		def __init__(self, *args, **kwargs):
			super(DiematicWebRequestHandler, self).__init__(*args, **kwargs)
		app = param
		boiler = app.MyBoiler
		parameter_names = _parameter_names(boiler)
	return DiematicWebRequestHandler

class DiematicLocalWebRequestHandler(BaseHTTPRequestHandler):
	""" 
		This class is a web server that provides GET and POST
		requests for the parameters of the boiler

		The parameters are defined in the same diematic.yaml file

		URL format:
		GET http://{host}/diematic/parameters
		returns a list of known parameters from the diematic.yaml

		GET http://{host}/diematic/parameters/{parameterName}
		return a JSON 
		{
			"paremeterName": value
		}

		POST http://{host}/diematic/parameters/{parameterName}
		body must contain a json
		{
			"parameterName": value
		}
	"""

	def _set_headers_json(self):
		self.send_response(200)
		self.send_header('Content-type', 'application/json')
		self.end_headers()

	def _set_headers_ok(self):
		self.send_response(200)
		self.end_headers()

	def _set_headers_html(self):
		self.send_response(200)
		self.send_header('Content-type', 'text/html; charset=utf-8')
		self.end_headers()

	def _set_error(self, message):
		self.send_response(404)
		self.send_header('Content-type', 'text/html')
		self.end_headers()
		self.wfile.write(bytes("<html><head><title>Diematic REST controller by IHR at home (Ignacio Hernández-Ros)</title></head>", "utf-8"))
		self.wfile.write(bytes("<body>", "utf-8"))
		self.wfile.write(bytes("<p>Request: %s</p>" % self.path, "utf-8"))
		self.wfile.write(bytes("<p>NOT FOUND {message!r}!</p>".format(message = message), "utf-8"))
		self.wfile.write(bytes("</body></html>", "utf-8"))

	def do_GET(self):
		""" returns parameters list or one parameter data in json format
			http://.../diematic/parameters
			http://.../diematic/parameters/{parameter_name}
		"""
		pathParts = self.path.split('/')
		if len(pathParts) < 3 or pathParts[1] != 'diematic':
			self._set_error('GET request FAILED. Try http://.../diematic/parameters to obtain the list of known parameter names')
		if len(pathParts) == 3 and 'parameters' == pathParts[2]:
			self.send_list()
		elif len(pathParts) == 3 and 'json' == pathParts[2]:
			self.send_json()
		elif len(pathParts) == 3 and 'config' == pathParts[2]:
			self.send_config()
		elif len(pathParts) == 4 and 'parameters' == pathParts[2] and pathParts[3] in self.parameter_names:
			self.send_param(pathParts[3])
		else:
			self._set_error('{path!r} is not a known request'.format(path=self.path))

	def do_POST(self):
		""" updates a value of one parameter in the boiler
			http://.../diematic/parameters/{parameter_name}
			the body shall contain json like this { "value": "12345" }
		"""
		pathParts = self.path.split('/')
		if len(pathParts) < 4:
			self._set_error('POST request FAILED. Try http://.../diematic/parameters to obtain the list of known parameter names')
			return
		valid1 = 'diematic' == pathParts[1]
		if not valid1:
			self._set_error('POST request FAILED. Try http://.../diematic/parameters/\{parameter_name\} in url and \{ "value": value \} in body. 1st path must be \'diematic\'')
			return
		valid2 = 'parameters' == pathParts[2]
		if not valid2:
			self._set_error('POST request FAILED. Try http://.../diematic/parameters/\{parameter_name\} in url and \{ "value": value \} in body. 2nd path must be \'parameters\'')
			return
		valid3 = pathParts[3] in self.parameter_names
		if not valid3:
			self._set_error('POST request FAILED. Try http://.../diematic/parameters/\{parameter_name\} in url and \{ "value": value \} in body. 3rd path must be one parameter name that is defined in the diematic.yaml file')
			return

		try:
			if len(pathParts) == 4:
				content_len = int(self.headers.get('content-length', 0))
				post_body_json = self.rfile.read(content_len).decode('utf8')
				jsoninput = json.loads(post_body_json)
				value = jsoninput['value']
				self.set_param(pathParts[3], value)
				self._set_headers_ok()
			elif len(pathParts) == 5 and 'resume' == pathParts[4]:
				""" clear previous error, no body is required """
				self.boiler.clear_error(pathParts[3])
				self._set_headers_ok()
			else:
				self._set_error('in order to clear an error, path must be http://.../diematic/parameters/\{parameter_name\}/resume ')
		except BaseException as error:
			self._set_error('POST request FAILED. Error {error}'.format(error=error))

	def set_param(self, paramName, paramValue):
		""" generates a request to update the value by writing in the registers
		"""
		self.boiler.set_write_pending(paramName, paramValue)
		self.app.check_pending_writes()

	def send_list(self):
		""" produces a list of well known register names 
		"""
		self._set_headers_html()
		self.wfile.write(bytes("<html><head><title>Diematic REST controller by IHR at home (Ignacio Hernández-Ros)</title></head>", "utf-8"))
		self.wfile.write(bytes("<body>", "utf-8"))
		self.wfile.write(bytes("<p>Recognized parameters list</p>", "utf-8"))
		self.wfile.write(bytes("<ul>", "utf-8"))
		for name in self.parameter_names:
			self.wfile.write(bytes("<li><a href='/diematic/parameters/{name}'>{name}</a></li>".format(name=name), "utf-8"))
		self.wfile.write(bytes("</ul>", "utf-8"))
		self.wfile.write(bytes("</body></html>", "utf-8"))

	def send_param(self, param_name):
		self._set_headers_json()
		self.wfile.write(bytes(json.dumps(getattr(self.boiler, param_name)), "utf-8"))

	def send_json(self):
		self._set_headers_json()
		self.wfile.write(bytes(self.boiler.toJSON(),"utf-8"))

	def send_config(self):
		self._set_headers_json()
		self.wfile.write(bytes(self.app.toJSON(),"utf-8"))