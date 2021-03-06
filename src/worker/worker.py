#!/usr/bin/env python3

import os
import sys
import socket
import json
import urllib.request, urllib.parse, urllib.error
import time
import calendar
import asyncore
from pprint import pprint, pformat
import logging
import signal
from Crypto.PublicKey import RSA
from base64 import b64decode
from worker_fetcher import NwFetcher, SaFetcher
from worker_communicator import Communicator
try:
  import errno
except ImportError:
  errno = os.errno

fetcher = None



def systemdLevelFormatter(level):
  if level == 50: # CRITICAL or FATAL
    return '<2>'
  elif level == 40: # ERROR
    return '<3>'
  elif level == 30: # WARNING or WARN
    return '<4>'
  elif level == 20: # INFO
    return '<6>'
  elif level == 10: # DEBUG
    return '<7>'



class SystemdFormatter(logging.Formatter):

  def __init__(self, fmt=None, datefmt=None):
    logging.Formatter.__init__(self, fmt, datefmt)

  def formatOld(self, record):
    return logging.Formatter.format(self, record)

  def format(self, record):
    record.message = record.getMessage()
    if self.usesTime():
      record.asctime = self.formatTime(record, self.datefmt)
    try:
      #s = self._fmt % record.__dict__
      s = systemdLevelFormatter(record.levelno) + self._fmt % record.__dict__
    except UnicodeDecodeError as e:
      # Issue 25664. The logger name may be Unicode. Try again ...
      try:
        record.name = record.name.decode('utf-8')
        #s = self._fmt % record.__dict__
        s = systemdLevelFormatter(record.levelno) + self._fmt % record.__dict__
      except UnicodeDecodeError:
        raise e

    if record.exc_info:
      # Cache the traceback text to avoid converting it multiple times
      # (it's constant anyway)
      if not record.exc_text:
        record.exc_text = self.formatException(record.exc_info)

    if record.exc_text:
      if s[-1:] != "\n":
        s = s + "\n"
      try:
        s = s + record.exc_text
      except UnicodeError:
        # Sometimes filenames have non-ASCII chars, which can lead
        # to errors when s is Unicode and record.exc_text is str
        # See issue 8924.
        # We also use replace for when there are multiple
        # encodings, e.g. UTF-8 for the filesystem and latin-1
        # for a script. See issue 13232.
        s = s + record.exc_text.decode(sys.getfilesystemencoding(), 'replace')
    return s

#raise Exception('Some exception')



def sigIntHandler(signal, frame):
  log.info("Worker terminated cleanly by interrupt")
  log.info("Exiting with code 0")
  global fetcher
  if fetcher:
    fetcher.terminate()
  sys.exit(0)



def pkcs1_unpad(text):
  if len(text) > 0 and text[0] == 2:
    # Find end of padding marked by null byte
    pos = text.find(0)
    if pos > 0:
      return text[pos+1:]
  return None



def configReceived(cfgObj):
  global fetcher
  try:

    try:
      cfg = cfgObj['workerConfig']
      log.debug('configReceived: cfg:\n' + pformat(cfg))

      serviceType = cfg['serviceType']

      #decrypt password
      ePassword = cfg['password']
      privateKeyFile = cfg['privateKeyFile']
      rsaKey = RSA.importKey(open(privateKeyFile, "rb").read())
      rawCipherData = b64decode(ePassword)
      cfg['dpassword'] = str(pkcs1_unpad(rsaKey.decrypt(rawCipherData)), 'utf-8') # write decrypted password back to config

      # Leave these
      collectionId = cfg['collectionId']
      id = cfg['id']
      state = cfg['state']
      collectionsDir = cfg['collectionsDir']
      
      if serviceType == 'nw': # NetWitness time and query clauses
        '''select * where (time='2017-May-02 14:00:00'-'2017-May-02 14:59:59') && (vis.level exists)'''
        #query = '''select * where (time='%s'-'%s') && (vis.level exists)''' % (oneHourAgoStr, curtimeStr)
        #query = '''select sessionid where (time='%s'-'%s') && (vis.level exists)''' % (oneHourAgoStr, curtimeStr)
        timeformatter='%Y-%B-%d %H:%M:%S'
        timeBegin = time.gmtime( cfg['timeBegin'] )
        timeBeginStr = time.strftime(timeformatter, timeBegin)
        timeEnd = time.gmtime( cfg['timeEnd'] )
        timeEndStr = time.strftime(timeformatter,timeEnd)
        timeClause = "time='%s'-'%s'" % (timeBeginStr, timeEndStr)
        log.debug("timeClause: " + timeClause)

        query = 'select * where (%s) && (%s)' % (timeClause, cfg['query'])
        cfg['queryEnc'] = urllib.parse.quote_plus(query)
        log.info("Query: " + query)
        #log.debug("queryEnc: " + cfg['queryEnc'])
      
      if serviceType == 'sa': # Solera time and query clauses
        timeformatter='%Y-%m-%dT%H:%M:%S-00'
        timeBegin = time.gmtime( cfg['timeBegin'])
        timeBeginStr = time.strftime(timeformatter, timeBegin)
        log.debug('timeBegin: ' + timeBeginStr)
        timeEnd = time.gmtime( cfg['timeEnd'])
        timeEndStr = time.strftime(timeformatter,timeEnd)
        log.debug('timeEnd: ' + timeEndStr)
        cfg['time1'] = timeBeginStr
        cfg['time2'] = timeEndStr
        q = cfg['query']
        query = q.split()
        cfg['saQuery'] = query


      outputDir = collectionsDir + '/' + id
      cfg['outputDir'] = outputDir
      #os.makedirs(outputDir)
      try:
        os.makedirs(outputDir)
      except OSError as e:
        if e.errno != errno.EEXIST:
          raise   
        pass


    except KeyError as e:
      error = 'ERROR: Missing critical configuration data: ' + str(e)
      exitWithError(error)
  
    signal.signal(signal.SIGINT, signal.SIG_IGN) # disable SIGINT handler before pool is created in fetcher constructor.  we don't want its threads catching ctrl-c


    if serviceType == 'nw':
      # NetWitness
      fetcher = NwFetcher(cfg, communicator)
      signal.signal(signal.SIGINT, sigIntHandler)  # restore signal handler

      ###QUERY DATA###
      log.info("Executing NetWitness query")
      communicator.write_data(json.dumps( { 'collection': { 'id': collectionId, 'state': 'querying' }} ) + '\n') #Tell communicator that we're querying
      time0 = time.time()
      communicator.write_data(json.dumps( { 'queryResultsCount': 0 } ) + '\n')
      numResults = fetcher.runQuery()
      log.info(str(numResults) + " sessions returned from query")
      time1 = time.time()
      log.info("Query completed in " + str(time1 - time0) + " seconds")

      communicator.write_data(json.dumps( { 'queryResultsCount': numResults } ) + '\n')

      ###PULL FILES###
      if (numResults > 0):
        log.info("Extracting files from sessions")
        communicator.write_data(json.dumps( { 'collection': { 'id': collectionId, 'state': state }} ) + '\n')
        time0 = time.time()
        fetcher.pullFiles()
        time1 = time.time()
        log.info("Pulled files in " + str(time1 - time0) + " seconds")
      
      communicator.handle_close()

    
    
    if serviceType == 'sa':
      # Solera
      fetcher = SaFetcher(cfg, communicator)
      signal.signal(signal.SIGINT, sigIntHandler)  # restore signal handler

      ###QUERY DATA###
      log.info("Executing SA query")
      communicator.write_data(json.dumps( { 'collection': { 'id': collectionId, 'state': 'querying' }} ) + '\n') #Tell communicator that we're querying
      numResults = fetcher.runQuery( )
      log.info(str(numResults) + " sessions returned from query")
      communicator.handle_close() # this will have to get moved into SaFetcher

  except Exception as e:
    #log.exception("Unhandled exception in configReceived() - exiting worker with code 1: " + str(e) )
    #communicator.handle_close()
    #sys.exit(1)
    error = "configReceived(): Unhandled exception.  Exiting worker with code 1: " + str(e)
    exitWithException(error)



def exitWithError(message):
  log.error(message)
  global fetcher
  if fetcher:
    fetcher.terminate()
  communicator.write_data(json.dumps( { 'error': message} ) + '\n')
  communicator.handle_close()
  sys.exit(1)



def exitWithException(message):
  log.exception(message)
  global fetcher
  if fetcher:
    fetcher.terminate()
  communicator.write_data(json.dumps( { 'error': message} ) + '\n')
  communicator.handle_close()
  sys.exit(1)



def main():
  if len(sys.argv) == 1:
    print("Argument must be a path to a UNIX socket")
    sys.exit(1)
  try:
    #Set up logging
    global log
    log = logging.getLogger()
    log.setLevel(logging.INFO)

    handler = logging.StreamHandler()
    formatStr = '%(asctime)s afb_worker    %(levelname)-10s %(message)s'
    if 'SYSTEMD' in os.environ:
      formatStr = 'afb_worker    %(message)s'
      formatter = SystemdFormatter(formatStr)

    formatter = logging.Formatter(formatStr)
    handler.setFormatter(formatter)
    log.addHandler(handler)
    
    try:
      LOG_LEVELS = {
        'info': logging.INFO,
        'warn': logging.WARN,
        'error': logging.ERROR,
        'debug': logging.DEBUG
      }
      logLevel = LOG_LEVELS[os.environ['LOG_LEVEL']]
      log.setLevel(logLevel)
      print(f'!!!{logLevel}')
    except:
      pass

    #Register handler for SIGINT
    signal.signal(signal.SIGINT, sigIntHandler)

    #Handle rest of startup
    log.info("afb_worker is starting")
    socketFile = sys.argv[1]
    global communicator
    communicator = Communicator(socketFile, configReceived)
    asyncore.loop(use_poll=True)
    log.info("Exiting afb_worker with code 0")
    sys.exit(0)
  except Exception as e:
    log.exception("Unhandled general exception: " + str(e) )