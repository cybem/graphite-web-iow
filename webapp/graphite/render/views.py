"""Copyright 2008 Orbitz WorldWide

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""
import csv
import math
import pytz
import requests
from datetime import datetime
from time import time, mktime
from random import shuffle
from httplib import CannotSendRequest
from urllib import urlencode
from urlparse import urlsplit, urlunsplit
from cgi import parse_qs
from cStringIO import StringIO
try:
  import cPickle as pickle
except ImportError:
  import pickle

from graphite.compat import HttpResponse
from graphite.util import getProfileByUsername, json, unpickle
from graphite.iow_util import get_tenant
from graphite.remote_storage import HTTPConnectionWithTimeout
from graphite.logger import log
from graphite.render.evaluator import evaluateTarget
from graphite.render.attime import parseATTime
from graphite.render.functions import PieFunctions
from graphite.render.hashing import hashRequest, hashData
from graphite.render.glyph import GraphTypes

from django.http import HttpResponseServerError, HttpResponseRedirect
from django.template import Context, loader
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from datetime import timedelta



def renderView(request):
  start = time()
  (graphOptions, requestOptions) = parseOptions(request)
  post_data  = {
    'from' : int(requestOptions['startTime'].strftime('%s')),
    'until' : int(requestOptions['endTime'].strftime('%s')),
    'tenant' : requestOptions['tenant'],
    'format' : requestOptions.get('format'),
    'target': [t for t in requestOptions['targets'] if t.strip()],
    'tz': str(requestOptions['tzinfo']),
    'graphType': str(requestOptions['graphType']),
    'pieMode': str(requestOptions['pieMode']),
  }

  if 'maxDataPoints' in requestOptions:
    post_data['maxDataPoints'] = str(requestOptions['maxDataPoints'])

  post_data.update(graphOptions)
  log.rendering(post_data)
  servers = settings.RENDERING_HOSTS[:] #make a copy so we can shuffle it safely
  shuffle(servers)
  for server in servers:
    start2 = time()
    try:
      response = requests.post("%s/render/" % server, data=post_data)
      assert response.status_code == 200, "Bad response code %d from %s" % (response.status_code,server)
      contentType = response.headers['Content-Type']
      imageData = response.content
      assert imageData, "Received empty response from %s" % server
      # Wrap things up
      log.rendering('Remotely rendered image on %s in %.6f seconds' % (server,time() - start2))
      log.rendering('Spent a total of %.6f seconds doing remote rendering work' % (time() - start))
    except:
      log.exception("Exception while attempting remote rendering request on %s" % server)
      log.rendering('Exception while remotely rendering on %s wasted %.6f' % (server,time() - start2))
      continue

  response = buildResponse(imageData, contentType)
  log.rendering('Total rendering time %.6f seconds' % (time() - start))
  return response


def parseOptions(request):
  queryParams = request.REQUEST

  # Start with some defaults
  graphOptions = {'width' : 330, 'height' : 250}
  requestOptions = {}

  graphType = queryParams.get('graphType','line')
  assert graphType in GraphTypes, "Invalid graphType '%s', must be one of %s" % (graphType,GraphTypes.keys())
  graphClass = GraphTypes[graphType]

  # Fill in the requestOptions
  requestOptions['graphType'] = graphType
  requestOptions['graphClass'] = graphClass
  requestOptions['pieMode'] = queryParams.get('pieMode', 'average')
  requestOptions['cacheTimeout'] = int( queryParams.get('cacheTimeout', settings.DEFAULT_CACHE_DURATION) )
  requestOptions['targets'] = []

  # Extract the targets out of the queryParams
  mytargets = []
  # Normal format: ?target=path.1&target=path.2
  if len(queryParams.getlist('target')) > 0:
    mytargets = queryParams.getlist('target')

  # Rails/PHP/jQuery common practice format: ?target[]=path.1&target[]=path.2
  elif len(queryParams.getlist('target[]')) > 0:
    mytargets = queryParams.getlist('target[]')

  # Collect the targets
  for target in mytargets:
    requestOptions['targets'].append(target)

  if 'pickle' in queryParams:
    requestOptions['format'] = 'pickle'
  if 'rawData' in queryParams:
    requestOptions['format'] = 'raw'
  if 'format' in queryParams:
    requestOptions['format'] = queryParams['format']
    if 'jsonp' in queryParams:
      requestOptions['jsonp'] = queryParams['jsonp']
  if 'noCache' in queryParams:
    requestOptions['noCache'] = True
  if 'maxDataPoints' in queryParams and queryParams['maxDataPoints'].isdigit():
    requestOptions['maxDataPoints'] = int(queryParams['maxDataPoints'])

  requestOptions['tenant'] = get_tenant(request)

  requestOptions['localOnly'] = queryParams.get('local') == '1'

  # Fill in the graphOptions
  for opt in graphClass.customizable:
    if opt in queryParams:
      val = queryParams[opt]
      if (val.isdigit() or (val.startswith('-') and val[1:].isdigit())) and 'color' not in opt.lower():
        val = int(val)
      elif '.' in val and (val.replace('.','',1).isdigit() or (val.startswith('-') and val[1:].replace('.','',1).isdigit())):
        val = float(val)
      elif val.lower() in ('true','false'):
        val = val.lower() == 'true'
      elif val.lower() == 'default' or val == '':
        continue
      graphOptions[opt] = val

  tzinfo = pytz.timezone(settings.TIME_ZONE)
  if 'tz' in queryParams:
    try:
      tzinfo = pytz.timezone(queryParams['tz'])
    except pytz.UnknownTimeZoneError:
      pass
  requestOptions['tzinfo'] = tzinfo

  # Get the time interval for time-oriented graph types
  if graphType == 'line' or graphType == 'pie':
    if 'until' in queryParams:
      untilTime = parseATTime(queryParams['until'], tzinfo)
    else:
      untilTime = parseATTime('now', tzinfo) - timedelta(minutes=1)
    if 'from' in queryParams:
      fromTime = parseATTime(queryParams['from'], tzinfo)
    else:
      fromTime = parseATTime('-1d', tzinfo)

    startTime = min(fromTime, untilTime)
    endTime = max(fromTime, untilTime)
    assert startTime != endTime, "Invalid empty time range"

    requestOptions['startTime'] = startTime
    requestOptions['endTime'] = endTime

  return (graphOptions, requestOptions)


connectionPools = {}

def delegateRenderIOW(graphOptions,graphType,tenant):
  start = time()
  log.rendering(graphOptions['data'])
  post_data = {'target': [], 'from': '-24hours'}
  if 'data' in graphOptions:
    for series in graphOptions['data']:
      log.rendering(series.name)
      post_data['target'].append(series.name)
      post_data['from'] = series.start
      post_data['until'] = series.end
  post_data['tenant']=tenant
  post_data['graphType']=graphType
  servers = settings.RENDERING_HOSTS[:] #make a copy so we can shuffle it safely
  shuffle(servers)
  for server in servers:
    start2 = time()
    try:
      response = requests.post("%s/render/" % server, data=post_data)
      assert response.status_code == 200, "Bad response code %d from %s" % (response.status_code,server)
      contentType = response.headers['Content-Type']
      imageData = response.content
      assert contentType == 'image/png', "Bad content type: \"%s\" from %s" % (contentType,server)
      assert imageData, "Received empty response from %s" % server
      # Wrap things up
      log.rendering('Remotely rendered image on %s in %.6f seconds' % (server,time() - start2))
      log.rendering('Spent a total of %.6f seconds doing remote rendering work' % (time() - start))
      return imageData
    except:
      log.exception("Exception while attempting remote rendering request on %s" % server)
      log.rendering('Exception while remotely rendering on %s wasted %.6f' % (server,time() - start2))
      continue

def delegateRendering(graphType, graphOptions):
  start = time()
  postData = graphType + '\n' + pickle.dumps(graphOptions)
  servers = settings.RENDERING_HOSTS[:] #make a copy so we can shuffle it safely
  shuffle(servers)
  for server in servers:
    start2 = time()
    try:
      # Get a connection
      try:
        pool = connectionPools[server]
      except KeyError: #happens the first time
        pool = connectionPools[server] = set()
      try:
        connection = pool.pop()
      except KeyError: #No available connections, have to make a new one
        connection = HTTPConnectionWithTimeout(server)
        connection.timeout = settings.REMOTE_RENDER_CONNECT_TIMEOUT
      # Send the request
      try:
        connection.request('POST','/render/', postData)
      except CannotSendRequest:
        connection = HTTPConnectionWithTimeout(server) #retry once
        connection.timeout = settings.REMOTE_RENDER_CONNECT_TIMEOUT
        connection.request('POST', '/render/', postData)
      # Read the response
      response = connection.getresponse()
      assert response.status == 200, "Bad response code %d from %s" % (response.status,server)
      contentType = response.getheader('Content-Type')
      imageData = response.read()
      assert contentType == 'image/png', "Bad content type: \"%s\" from %s" % (contentType,server)
      assert imageData, "Received empty response from %s" % server
      # Wrap things up
      log.rendering('Remotely rendered image on %s in %.6f seconds' % (server,time() - start2))
      log.rendering('Spent a total of %.6f seconds doing remote rendering work' % (time() - start))
      pool.add(connection)
      return imageData
    except:
      log.exception("Exception while attempting remote rendering request on %s" % server)
      log.rendering('Exception while remotely rendering on %s wasted %.6f' % (server,time() - start2))
      continue


def renderLocalView(request):
  try:
    start = time()
    reqParams = StringIO(request.body)
    graphType = reqParams.readline().strip()
    optionsPickle = reqParams.read()
    reqParams.close()
    graphClass = GraphTypes[graphType]
    options = unpickle.loads(optionsPickle)
    image = doImageRender(graphClass, options)
    log.rendering("Delegated rendering request took %.6f seconds" % (time() -  start))
    return buildResponse(image)
  except:
    log.exception("Exception in graphite.render.views.rawrender")
    return HttpResponseServerError()


def renderMyGraphView(request,username,graphName):
  profile = getProfileByUsername(username)
  if not profile:
    return errorPage("No such user '%s'" % username)
  try:
    graph = profile.mygraph_set.get(tenat=get_tenant(request),name=graphName)
  except ObjectDoesNotExist:
    return errorPage("User %s doesn't have a MyGraph named '%s'" % (username,graphName))

  request_params = dict(request.REQUEST.items())
  if request_params:
    url_parts = urlsplit(graph.url)
    query_string = url_parts[3]
    if query_string:
      url_params = parse_qs(query_string)
      # Remove lists so that we can do an update() on the dict
      for param, value in url_params.items():
        if isinstance(value, list) and param != 'target':
          url_params[param] = value[-1]
      url_params.update(request_params)
      # Handle 'target' being a list - we want duplicate &target params out of it
      url_param_pairs = []
      for key,val in url_params.items():
        if isinstance(val, list):
          for v in val:
            url_param_pairs.append( (key,v) )
        else:
          url_param_pairs.append( (key,val) )

      query_string = urlencode(url_param_pairs)
    url = urlunsplit(url_parts[:3] + (query_string,) + url_parts[4:])
  else:
    url = graph.url
  return HttpResponseRedirect(url)


def doImageRender(graphClass, graphOptions):
  pngData = StringIO()
  t = time()
  img = graphClass(**graphOptions)
  img.output(pngData)
  log.rendering('Rendered PNG in %.6f seconds' % (time() - t))
  imageData = pngData.getvalue()
  pngData.close()
  return imageData


def buildResponse(imageData, content_type="image/png"):
  response = HttpResponse(imageData, content_type=content_type)
  response['Cache-Control'] = 'no-cache'
  response['Pragma'] = 'no-cache'
  return response


def errorPage(message):
  template = loader.get_template('500.html')
  context = Context(dict(message=message))
  return HttpResponseServerError( template.render(context) )
