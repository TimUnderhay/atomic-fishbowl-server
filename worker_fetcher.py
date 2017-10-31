import os
import sys
import urllib2
import ssl
import base64
import json
import email
import mimetypes
from pprint import pprint, pformat
import StringIO
from PIL import Image ##install
from worker_communicator import communicator
import time
from subprocess import Popen, PIPE
import zipfile
import shutil
import magic ##install
import hashlib
import rarfile ##install
import re
import logging
from worker_contentobj import ContentObj
from copy import deepcopy, copy
from multiprocessing import Pool, Manager, Value
import shlex
import socket

log = logging.getLogger(__name__)

def unwrapExtractFilesFromMultipart(*arg, **kwarg):
  #print "unwrapExtractFilesFromMultipart()"
  return ContentProcessor.extractFilesFromMultipart(*arg, **kwarg)

def unwrapPullFiles(*arg, **kwarg):
  #print "unwrapExtractFilesFromMultipart()"
  return ContentProcessor.pullFiles(*arg, **kwarg)

class Fetcher:

  def __init__(self, communicator, collectionId, url, user, password, directory, minX, minY, gsPath, pdftotextPath, unrarPath, contentLimit, summaryTimeout, queryTimeout, contentTimeout, maxContentErrors):

    self.pool = Pool()
    self.manager = Manager()

    self.communicator = communicator
    self.collectionId = collectionId

    self.url = url
    self.user = user
    self.password = password
    self.summary = {}
    self.sessions = {}
    self.directory = directory
    self.minX = int(minX)
    self.minY = int(minY)
    self.gsPath = gsPath
    self.pdftotextPath = pdftotextPath
    rarfile.UNRAR_TOOL = unrarPath
    self.contentLimit = contentLimit
    self.contentCount = self.manager.Value('I', 0)
    self.thumbnailSize = 350, 350
    self.devmode = True
    self.summaryTimeout = summaryTimeout
    self.queryTimeout = queryTimeout
    self.contentTimeout = contentTimeout
    self.maxContentErrors = maxContentErrors # should default to 6
    self.contentErrors = self.manager.Value('I', 0)
    log.info("Minimum dimensions are: " + str(minX) + " x " + str(minY))

  """curl "http://admin:netwitness@172.16.0.55:50104/sdk?msg=query&query=$query&force-content-type=application/json"""

  def exitWithError(self, message):
    log.error(message)
    self.communicator.write_data(json.dumps( { 'error': message} ) + '\n')
    self.communicator.handle_close()
    sys.exit(1)

  def exitWithException(self, message):
    log.exception(message)
    self.communicator.write_data(json.dumps( { 'error': message} ) + '\n')
    self.communicator.handle_close()
    sys.exit(1)

  def fetchSummary(self):
    request = urllib2.Request(self.url + '/sdk?msg=summary')
    base64string = base64.b64encode('%s:%s' % (self.user, self.password))
    request.add_header("Authorization", "Basic %s" % base64string)
    request.add_header('Content-type', 'application/json')
    request.add_header('Accept', 'application/json')
    summaryResult = json.load(urllib2.urlopen(request, timeout=self.summaryTimeout))
    for e in summaryResult['string'].split():
      (k, v) = e.split('=')
      self.summary[k] = v



  def runQuery(self, query):
    reqStr = self.url + '/sdk?msg=query&query=' + query  #&flags=4096
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    request = urllib2.Request(reqStr)
    base64string = base64.b64encode('%s:%s' % (self.user, self.password))
    request.add_header("Authorization", "Basic %s" % base64string)
    request.add_header('Content-type', 'application/json')
    request.add_header('Accept', 'application/json')
    try:
      rawQueryRes = json.load(urllib2.urlopen(request, context=ctx, timeout=self.queryTimeout))
      #pprint(rawQueryRes)
      #print "length of rawQueryRes", len(rawQueryRes)
      log.debug('runQuery(): Parsing query results')
      for field in rawQueryRes:
        #print "loop"
        if 'results' in field and isinstance(field, dict):
          if 'fields' in field['results']:
            metaList = field['results']['fields']
            for meta in metaList:
              
              metaKey = meta['type']
              metaValue = meta['value']
              sessionId = meta['group']

              if sessionId in self.sessions:
                thisSession = self.sessions[sessionId]
              else:
                thisSession = { 'images': [], 'session': { 'id': sessionId, 'meta': {} } }

              #if not sessionId in self.sessions:
              #  self.sessions[sessionId] = { 'images': [], 'session': { 'id': sessionId, 'meta': {} } }
              
              #if not key in self.sessions[sessionId]['session']['meta']:
              #  self.sessions[sessionId]['session']['meta'][key] = []
              if not metaKey in thisSession['session']['meta']:
                thisSession['session']['meta'][metaKey] = []
              
              #self.sessions[sessionId]['session']['meta'][key].append(value)
              thisSession['session']['meta'][metaKey].append(metaValue)
              
              #Update dict
              self.sessions[sessionId] = thisSession

    except urllib2.HTTPError as e:
      error = "runQuery(): HTTP Error whilst running query.  Exiting with code 1: " + str(e)
      self.exitWithError(error)
    except urllib2.URLError as e:
      if 'Connection refused' in str(e):
        error = "Connection refused whilst trying to query NetWitness service"
      else:
        error = "runQuery(): URL Error whilst running query.  Exiting with code 1: "  + str(e)
      self.exitWithError(error)
    except socket.timeout as e:
      error = "Query timed out after " + str(self.queryTimeout) + " seconds"
      self.exitWithError(error)
    except Exception as e:
      error = "runQuery(): Unhandled exception whilst running query.  Exiting with code 1: " + str(e)
      #log.exception("runQuery(): Unhandled exception whilst running query.  Exiting with code 1")
      #sys.exit(1)
      self.exitWithException(error)
    return len(self.sessions)



  def sendResult(self, session):
    #log.debug('sendResult()')
    if len(session['images']) != 0:
      log.debug("sendResult(): Worker sending update")
      self.communicator.write_data(json.dumps( { 'collectionUpdate': session } ) + '\n')


  def pullFiles(self, distillationTerms, regexDistillationTerms, md5Hashes=[], sha1Hashes=[], sha256Hashes=[]):

    for sessionId in self.sessions:
      if self.contentCount.value >= self.contentLimit:
        log.info("Image limit of " + str(self.contentLimit) + " has been reached.  Ending collection build.  You may want to narrow your result set with a more specific query")
        return
      elif not self.contentCount.value >= self.contentLimit:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        uri = '/sdk/content?render=107&session=' + str(sessionId)
        request = urllib2.Request(self.url + uri )
        base64string = base64.b64encode('%s:%s' % (self.user, self.password))
        request.add_header("Authorization", "Basic %s" % base64string)
        request.add_header('Content-type', 'application/json')
        request.add_header('Accept', 'application/json')
        error = ''
        #while self.contentErrors.value < self.maxContentErrors:
        try:
          res = urllib2.urlopen(request, context=ctx, timeout=self.contentTimeout)
          #break
        except urllib2.HTTPError as e:
          self.contentErrors.value += 1
          error = "HTTP exception pulling content for session " + str(sessionId) + ".  URI was '" + uri + "'.  The HTTP status code was " + str(e.code)
          log.error("pullFiles(): " + error )
          continue
        except urllib2.URLError as e:
          self.contentErrors.value += 1
          error = "URL error pulling content for session " + str(sessionId) + ".  The reason was " + e.reason
          log.error("pullFiles(): " + error)
          continue
        except socket.timeout as e:
          self.contentErrors.value += 1
          error = "content call for session " + str(sessionId) + " timed out after " + str(self.contentTimeout) + " seconds"
          log.error("pullFiles(): " + error)
          continue


        if self.contentErrors.value == self.maxContentErrors:
          e = "pullFiles(): Maximum retries reached whilst pulling files for session " + str(sessionId) + ".  The last error was " + error + ".  Exiting with code 1"
          self.exitWithError(e)

        if 'res' in locals() and res.info().getheader('Content-Type').startswith('multipart/mixed'):
          contentType = res.info().getheader('Content-Type')
          mimeVersion = res.info().getheader('Mime-Version')
          newFileStr = 'Content-Type: ' + contentType + '\n' + 'Mime-Version: ' + mimeVersion + '\n' + res.read()
          
          ##############EXTRACT FILES AND DO THE WORK##############
          log.debug('Launching extractor from pool')
          #processor = ContentProcessor(self.directory, self.minX, self.minY, self.gsPath, self.pdftotextPath, self.contentLimit, self.contentCount)
          processor = ContentProcessor(self.url, self.user, self.password, self.directory, self.minX, self.minY, self.gsPath, self.pdftotextPath, self.contentLimit, self.contentCount, self.contentErrors, self.maxContentErrors, self.contentTimeout)
          self.pool.apply_async(unwrapExtractFilesFromMultipart, args=(processor, newFileStr, self.sessions[sessionId], sessionId, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes), callback=self.sendResult )
      



    self.pool.close()
    self.pool.join()





  def newpullFiles(self, distillationTerms, regexDistillationTerms, md5Hashes=[], sha1Hashes=[], sha256Hashes=[]):
    #This tries to put every content call in its own process but this is actually slower than handling the content call in a single-threaded manner
    for sessionId in self.sessions:
      if not self.contentCount.value >= self.contentLimit:
        processor = ContentProcessor(self.url, self.user, self.password, self.directory, self.minX, self.minY, self.gsPath, self.pdftotextPath, self.contentLimit, self.contentCount, self.contentErrors, self.maxContentErrors, self.contentTimeout)

        ##############PULL FILES AND PROCESS THEM##############
        log.debug('Launching extractor from pool')
        res = self.pool.apply_async(unwrapPullFiles, args=(processor, self.sessions[sessionId], sessionId, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes), callback=self.sendResult )
        res.get()

      else:
        log.info("Image limit of " + str(self.contentLimit) + " has been reached.  Ending collection build.  You may want to narrow your result set with a more specific query")
        return
    
   

























class ContentProcessor:

  def __init__(self, url, user, password, directory, minX, minY, gsPath, pdftotextPath, contentLimit, contentCount, contentErrors, maxContentErrors, timeout):
    self.url = url
    self.directory = directory
    self.minX = int(minX)
    self.minY = int(minY)
    self.gsPath = gsPath
    self.pdftotextPath = pdftotextPath
    self.contentLimit = contentLimit
    self.contentCount = contentCount # is a manager proxy
    self.thumbnailSize = 350, 350
    self.devmode = True
    self.contentErrors = contentErrors # is a manager proxy
    self.maxContentErrors = maxContentErrors
    self.timeout = timeout
    self.user = user
    self.password = password


  def pullFiles(self, session, sessionId, distillationTerms, regexDistillationTerms, md5Hashes=[], sha1Hashes=[], sha256Hashes=[]):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    request = urllib2.Request(self.url + '/sdk/content?render=107&session=' + str(sessionId))
    base64string = base64.b64encode('%s:%s' % (self.user, self.password))
    request.add_header("Authorization", "Basic %s" % base64string)
    request.add_header('Content-type', 'application/json')
    request.add_header('Accept', 'application/json')
    while self.contentErrors.value < self.maxContentErrors:
      try:
        res = urllib2.urlopen(request, context=ctx, timeout=self.timeout)
        break
      except urllib2.HTTPError as e:
        if self.contentErrors.value == self.maxContentErrors:
          log.error("pullFiles(): Maximum retries reached whilst pulling content for session " + str(sessionId) + ".  Exiting with code 1")
          sys.exit(1)
        self.contentErrors.value += 1
        log.error("pullFiles(): HTTP error pulling content for session " + str(sessionId) + ".  Retrying")
        continue
      except urllib2.URLError as e:
        if self.contentErrors.value == self.maxContentErrors:
          log.error("pullFiles(): Maximum retries reached whilst pulling content for session " + str(sessionId) + ".  Exiting with code 1")
          sys.exit(1)
        self.contentErrors.value += 1
        log.error("pullFiles(): ERROR: URL error pulling content for session " + str(sessionId) + ".  Retrying")
        continue

    if res.info().getheader('Content-Type').startswith('multipart/mixed'):
      contentType = res.info().getheader('Content-Type')
      mimeVersion = res.info().getheader('Mime-Version')
      newFileStr = 'Content-Type: ' + contentType + '\n' + 'Mime-Version: ' + mimeVersion + '\n' + res.read()
      
      ##############EXTRACT FILES AND DO THE WORK##############
      #log.debug('Launching extractor from pool')
      #processor = ContentProcessor(self.directory, self.minX, self.minY, self.gsPath, self.pdftotextPath, self.contentLimit, self.contentCount)
      #self.pool.apply_async(unwrapExtractFilesFromMultipart, args=(processor, newFileStr, self.sessions[sessionId], sessionId, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes), callback=self.sendResult )
      return self.extractFilesFromMultipart(newFileStr, session, sessionId, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes)
        

  
  def processImage(self, contentObj): #must specify either part or stringFile
    log.debug("processImage(): Analyzing image " + contentObj.contentFile)
    contentObj.contentType = 'image'
    output = contentObj.getFileContent()
      
    try:
      im = Image.open(output)
      (x,y) = im.size
    except Exception as e:
      log.warning("Could not identify image file " + contentObj.contentFile + ".  This is likely due to file corruption")
      return False
      
    #check file for dimensions and only write if minimum
    if x >= int(self.minX) and y >= int(self.minY):
      log.debug("processImage(): Keeping image " + contentObj.contentFile + " of resolution " + str(x) + 'x' + str(y) )
      fp = open(os.path.join(self.directory, contentObj.contentFile), 'wb')
      fp.write(output.getvalue())
      fp.close()

      log.debug("processImage(): Generating thumbnail for image " + contentObj.contentFile)
      thumbnailName = 'thumbnail_' + contentObj.contentFile
      
      try:
        im.thumbnail(self.thumbnailSize, Image.ANTIALIAS)
      except IOError as e: #we don't want to keep corrupt files.  we know it's corrupt if we can't generate a thumbnail
        log.warning("Image appears to be corrupt: " + contentObj.contentFile)
        return False
      except Exception as e:
        log.exception("Unhandled exception whilst generating thumbnail")
        return False
      
      im.save(os.path.join(self.directory, thumbnailName), im.format)
      contentObj.thumbnail = thumbnailName
      
      self.contentCount.value += 1

      #self.sessions[contentObj.session]['images'].append( contentObj.get() )
      self.thisSession['images'].append( contentObj.get() )
      

    else:
      log.debug("processImage(): Discarding image " + contentObj.contentFile + " due to minimum size requirements")
      return False
    
    return True

  


  def processPdf(self, contentObj, distillationTerms, regexDistillationTerms):
    contentObj.contentType = 'pdf'
    #log.debug('processPdf(): contentObj:' + pformat(contentObj.get()) )

    #write pdf to disk
    log.debug("processPdf(): Writing pdf to " + os.path.join(self.directory, contentObj.contentFile) )
    fp = open(os.path.join(self.directory, contentObj.contentFile), 'wb')
    shutil.copyfileobj(contentObj.getFileContent(), fp)
    fp.close()
    
    #extract first page of pdf
    #gs -dNOPAUSE -sDEVICE=jpeg -r144 -sOutputFile="p%o3d.jpg" -dFirstPage=1 -dLastPage=1 -dBATCH "$filename"
    log.debug("processPdf(): Extracting first page of pdf " + contentObj.contentFile)
    outputfile = "page1-" + contentObj.contentFile + ".jpg"
    contentObj.pdfImage = outputfile
    log.debug("processPdf(): Running gs on file " + contentObj.contentFile)
    
    
    gsCmd = self.gsPath + " -dNOPAUSE -sDEVICE=jpeg -r144 -sOutputFile='" + os.path.join(self.directory, outputfile) + "' -dPDFSTOPONERROR -dFirstPage=1 -dLastPage=1 -dBATCH '" +  os.path.join(self.directory, contentObj.contentFile) + "'"
    log.debug("processPdf(): Ghostscript command line: " + gsCmd)
    args = shlex.split(gsCmd)
    try:
      #process = Popen(gsCmd, stdout=PIPE, stderr=PIPE, shell = True)
      process = Popen(args, stdout=PIPE, stderr=PIPE, shell = False)
      (output, err) = process.communicate()
      exit_code = process.wait()
    
    except OSError as e:
      if ('No such file or directory' in str(e)):
        log.error('ERROR: Could not run ghostscript command as ' + self.gsPath + ' was not found')
      else:
        log.exception(str(e))
      return False

    except Exception as e:
      #Ghostscript couldn't even be run
      log.exception("ERROR: Could not run GhostScript command at " + self.gsPath )
      return False

    if exit_code != 0:
      #Ghostscript exited with a non-zero exit code, and thus was unsuccessful
      log.warning("WARNING: GhostScript exited abnormally with code " + str(exit_code) )
      return False

    if exit_code == 0: #this means we successfully generated an image of the pdf and we want to keep it
      keep = True
      ###keep means we want to keep the file.  We choose to not keep the file (by making keep = False) on these conditions:
      ###1: if an error is generated by gs whilst trying to render an image of the first page
      ###2: if we have distillationTerms and/or regexDistillationTerms and they aren't matched

      returnObj = self.getPdfText(contentObj, searchTerms=distillationTerms, regexSearchTerms=regexDistillationTerms)
      keep = returnObj['keep']
      contentObj = returnObj['contentObj']

      if keep == False:
        return False #return if we've chosen to not keep the file
      
      try:
        #now let's try generating a thumbnail - if we already have an image, this should succeed.  If not, there's something screwy...
        #but we'll keep it anyway and use the original image as the thumbnail and let the browser deal with any potential corruption
        log.debug("processPdf(): Generating thumbnail for pdf " + outputfile)
        thumbnailName = 'thumbnail_' + outputfile
        pdfim = Image.open(os.path.join(self.directory, outputfile))
        pdfim.thumbnail(self.thumbnailSize, Image.ANTIALIAS)
        pdfim.save(os.path.join(self.directory, thumbnailName), pdfim.format)
        #pdfim.close()

        #set thumbnail to our generated thumbnail
        contentObj.thumbnail = thumbnailName
        self.thisSession['images'].append( contentObj.get() )
        self.contentCount.value += 1
        return True
      except Exception as e:
        log.exception("Error generating thumbnail for pdf " + contentObj.contentFile)
        #thumbnail generation failed, so set thumbnail to be the original image generated by gs
        contentObj.thumbnail = outputfile
        self.thisSession['images'].append( contentObj.get() )
        self.contentCount.value += 1
        return True
        



  def getPdfText(self, contentObj, searchTerms=[], regexSearchTerms=[]):
    try: #now extract pdf text
      log.debug('getPdfText(): session: ' + str(contentObj.session))
      sessionId = contentObj.session

      pdftotextCmd = self.pdftotextPath + " -enc UTF-8 -eol unix -nopgbrk -q '" + os.path.join(self.directory, contentObj.contentFile) + "' -"
      log.debug("getPdfText(): pdftotextCmd: " + pdftotextCmd)
      args = shlex.split(pdftotextCmd)
      try:
        #pdftotextProcess = Popen(pdftotextCmd, stdout=PIPE, stderr=PIPE, shell = True)
        pdftotextProcess = Popen(args, stdout=PIPE, stderr=PIPE, shell = False)
        (output, err) = pdftotextProcess.communicate()
        exit_code = pdftotextProcess.wait()
      
      except OSError as e:
        if ('No such file or directory' in str(e)):
          log.error('ERROR: Could not run pdftotext command as ' + self.pdftotextPath + ' was not found')
        else:
          log.exception(str(e))
        if len(searchTerms) == 0 and len(regexSearchTerms) == 0:
          return { 'keep': True, 'contentObj': contentObj }
        return { 'keep': False, 'contentObj': contentObj }

      except Exception as e:
        log.exception("Could not run pdftotext command at " + self.pdftotextPath )
        if len(searchTerms) == 0 and len(regexSearchTerms) == 0:
          return { 'keep': True, 'contentObj': contentObj }
        return { 'keep': False, 'contentObj': contentObj }

      returnObj = { 'keep': False, 'contentObj': {} }
      
      textTermsMatched = []
      regexTermsMatched = []
      contentObj.textDistillationEnabled = False
      contentObj.regexDistillationEnabled = False
      
      if len(searchTerms) > 0:
        contentObj.textDistillationEnabled = True
      if len(regexSearchTerms) > 0:
        contentObj.regexDistillationEnabled = True

      if exit_code == 0:
        #extracted successfully, get output
        joinedText = output.replace('\n', ' ').replace('\r', '')

        for term in searchTerms:
          #log.debug( "getPdfText(): Text search term: " + term)
          if term.decode('utf-8').lower() in joinedText.decode('utf-8').lower():
            textTermsMatched.append(term)
            log.debug("getPdfText(): Matched text search term " + term)
        
        for t in regexSearchTerms:
          origTerm =  t.decode('utf-8')
          term = '(' + t.decode('utf-8') + ')';
          #log.debug( "getPdfText(): Regex search term: " + term)
          compiledTerm = re.compile(term)
          res = compiledTerm.search(joinedText.decode('utf-8')) #MatchObject
          if res != None:
            regexTermsMatched.append(origTerm)
            log.debug("getPdfText(): Matched regex search term " + term)
            log.debug("getPdfText(): Matched group: " + pformat(res.groups()))

        #'keep' is a variable that gets returned which indicates whether the document and session should be retained as part of a collection, if a term has been matched or if there were no terms
        if len(searchTerms) == 0 and len(regexSearchTerms) == 0: #no search terms defined - distillation is not enabled - definitely keep this session
          returnObj['keep'] = True
        if len(searchTerms) > 0 and len(textTermsMatched) > 0: #we had text search terms and had at least one match, so we will keep this session
          returnObj['keep'] = True
          contentObj.textTermsMatched = textTermsMatched
        if len(regexSearchTerms) > 0 and len(regexTermsMatched) > 0: #we had regex search terms and had at least one match, so we will keep this session
          returnObj['keep'] = True
          contentObj.regexTermsMatched = regexTermsMatched
        
        returnObj['contentObj'] = contentObj #pass our modified contentObj back to the caller

        if returnObj['keep'] == True:
          log.debug("getPdfText(): keeping file " + contentObj.contentFile)
          searchObj = { 'id': contentObj.id, 'session': sessionId, 'contentFile': contentObj.contentFile, 'searchString': joinedText }
          if not 'search' in self.thisSession:
            self.thisSession['search'] = []
          #pprint(self.thisSession['search'])
          self.thisSession['search'].append(searchObj)

        return returnObj
        
    except Exception as e:
      log.exception("Unhandled exception in getPdfText()")
      #print "Error Message:", str(e)
      #continue
      #if searchForText:
      #if len(searchForText) != 0:
      #  return True #keep session if pdftotext has issues.
      return returnObj


  def genHash(self, contentObj, hashes): #must specify either part or stringFile
    #print "genHash()"
    contentObj.contentType = 'hash'
    log.debug("genHash(): Generating " + contentObj.hashType + " hash for " + contentObj.contentFile)

    contentFileObj = contentObj.getFileContent()

    if contentObj.hashType == 'md5':
      hash = hashlib.md5()
    if contentObj.hashType == 'sha1':
      hash = hashlib.sha1()
    if contentObj.hashType == 'sha256':
      hash = hashlib.sha256()
    hash.update(contentFileObj.getvalue())
      
    log.debug("genHash(): " + contentObj.hashType + " hash for " + contentObj.contentFile + " is " + hash.hexdigest())

    for h in hashes:
      if hash.hexdigest().decode('utf-8').lower() == h['hash'].lower():
        log.debug("genHash(): Matched " + contentObj.hashType + " hash " + h['hash'])
        fp = open(os.path.join(self.directory, contentObj.contentFile), 'wb')
        fp.write(contentFileObj.getvalue())
        fp.close()
        ###imgObj = { 'session': sessionId, 'contentType': 'md5Matched', 'contentFile': filename, 'image': filename, 'md5Hash': hash_md5.hexdigest() }
        contentObj.hashValue = hash.hexdigest()
        if 'friendly' in h:
          contentObj.hashFriendly = h['friendly']
        self.thisSession['images'].append( contentObj.get() )

        


  def extractFilesFromMultipart(self, fileStr, session, sessionId, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes):
    log.debug("extractFilesFromMultipart(): Extracting files of session ID " + str(sessionId) )
    #print("extractFilesFromMultipart(): Extracting files of session ID " + str(sessionId) )
    msg = email.message_from_string(fileStr)
    counter = 1

    self.thisSession = session


    for part in msg.walk():

      # multipart/* are just containers
      if part.get_content_maintype() == 'multipart' and part.get_content_subtype() == 'mixed':
        continue
      elif part.get_content_maintype() == 'image':
        contentType = 'image'
      elif part.get_content_maintype() == 'application' and part.get_content_subtype() == 'pdf':
        contentType = 'pdf'
      else:
        log.debug("extractFilesFromMultipart(): Detecting archives")
        type = magic.from_buffer( part.get_payload(decode=True), mime=True)
        log.debug("extractFilesFromMultipart(): Magic type is " + type)
        if type == 'application/zip':
          contentType = 'zip'
        elif type == 'application/x-rar':
          contentType = 'rar' #confirm this type
        elif type in ['application/x-msdownload', 'application/x-ms-installer', 'application/x-elf', 'application/x-dosexec', 'application/x-executable']: #and len(md5Hashes) != 0: #fix this for known executable types
          contentType = 'executable'
        else: #should we make else generate hash instead of continue?  stick with just exe's for now
          continue
      
      # Applications should really sanitize the given filename so that an
      # email message can't be used to overwrite important files
      filename = part.get_filename().decode('utf-8')
      if not filename:
        log.debug("No filename")
        ext = mimetypes.guess_extension(part.get_content_type())
        if not ext:
          # Use a generic bag-of-bits extension
          ext = '.bin'
        filename = 'part-%03d%s' % (counter, ext)
      counter += 1
      
      #print filename, part.get_content_maintype(), part.get_content_subtype()

      if self.contentCount.value >= self.contentLimit:
        #return
        return self.thisSession



      ##############################################################################
      #We're going to keep the content - so now start doing things with the content#
      ##############################################################################
      contentObj = ContentObj()
      contentObj.session = contentObj.session = sessionId

      if contentType == 'image' and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0:
        contentObj.contentFile = filename
        contentObj.setPartContent(part)
        ###if not self.processImage(filename, sessionId, contentType, part=part):
        if not self.processImage(contentObj):
          continue
        
      elif contentType == 'pdf':
        contentObj.contentFile = filename
        contentObj.setPartContent(part)
        ###if not self.processPdf(filename, sessionId, contentType, distillationTerms, regexDistillationTerms, part=part):
        if not self.processPdf(contentObj, distillationTerms, regexDistillationTerms):
          continue
        
      elif contentType == 'executable':
        contentObj.contentFile = filename
        contentObj.setPartContent(part)
        if len(md5Hashes) != 0:
          contentObj.hashType = 'md5'
          self.genHash(contentObj, md5Hashes)
        if len(sha1Hashes) != 0:
          contentObj.hashType = 'sha1'
          self.genHash(contentObj, sha1Hashes)
        if len(sha256Hashes) != 0:
          contentObj.hashType = 'sha256'
          self.genHash(contentObj, sha256Hashes)
      


      ######################################################
      #################Process ZIP ARCHIVES#################
      ######################################################

      elif contentType == 'zip':
        log.debug("extractFilesFromMultipart(): Attempting to extract zip archive " + filename)
        contentObj.fromArchive = True
        contentObj.archiveType = 'zip'
        saveZipFile = False
        origContentObj = copy(contentObj)
        
        try:
          zipFileHandle = zipfile.ZipFile(self.convertPartToStringIO(part))
        except zipfile.BadZipfile as e:
          log.warning("Bad ZIP File (file was not opened): " + contentObj.archiveFilename)
        except zipfile.LargeZipFile as e:
          log.warning("ZIP file was too large to open: " + contentObj.archiveFilename)
        except NotImplementedError as e:
          log.exception("NotImplemented exception during zip open")
        except Exception as e:
          log.exception("Unhandled exception during zip file open")
         
        try:
          for zinfo in zipFileHandle.infolist():
            contentObj = copy(origContentObj)
            
            archivedFilename = zinfo.filename
            contentObj.contentFile = archivedFilename
            contentObj.archiveFilename = filename
            is_encrypted = zinfo.flag_bits & 0x1
            #print "DEBUG: zip compression type is",str(zinfo.compress_type)
            unsupported_compression = zinfo.compress_type == 99
            
            if is_encrypted and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0:
              saveZipFile = True
              log.debug('extractFilesFromMultipart(): ZIP contentFile %s from archive %s is encrypted!' % (archivedFilename, filename))
              contentObj.contentType = 'encryptedZipEntry'
              #contentObj.contentFile = filename #this is the zip file itself
              contentObj.fromArchive = True
              self.thisSession['images'].append( contentObj.get() )
              continue

            elif unsupported_compression and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0:
              saveZipFile = True
              log.debug('extractFilesFromMultipart(): ZIP archive %s uses an unsupported compression type!' % filename)
              contentObj.contentType = 'unsupportedZipEntry'
              contentObj.contentFile = filename #this is the zip file itself
              contentObj.fromArchive = False # if the file is unsupported, it becomes the content itself, not what's inside it,so it's not FROM an archive, it IS the archive
              contentObj.isArchive = True
              self.thisSession['images'].append( contentObj.get() )
              break
            
            else: #identify archived file and save it permanently if a supported file type
              extractedFileObj = StringIO.StringIO() #we will write the extracted file to this buffer

              #extract the file to buffer
              compressedFileHandle = zipFileHandle.open(archivedFilename) #compressedFileHandle is a handle to the file while it's still in the zip file.  we will extract from this object
              extractedFileObj.write(compressedFileHandle.read() )  #this is where we extract the file into
              compressedFileHandle.close()

              contentObj.setStringIOContent(extractedFileObj)
              self.processExtractedFile(contentObj, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes) #now let's process the extracted file
              extractedFileObj.close()
      
        except RuntimeError as e:
          if 'is encrypted, password required for extraction' in e.message:
            pass # do nothing and let our own code take the lead
          else:
            log.exception('Unhandled RuntimeError')
        except Exception as e:
          log.exception("Unhandled exception during zip file extraction")

        if saveZipFile:
          fp = open(os.path.join(self.directory, filename), 'wb')
          fp.write(part.get_payload(decode=True))
          fp.close()
        continue          



      #################Process RAR ARCHIVES#################
      elif contentType == 'rar':
        log.debug("extractFilesFromMultipart(): Attempting to extract rar archive " + filename)
        contentObj.fromArchive = True
        contentObj.archiveType = 'rar'
        saveRarFile = False
        origContentObj = copy(contentObj)
        
        try:
          rarFileHandle = rarfile.RarFile(self.convertPartToStringIO(part), errors='strict')
        except rarfile.PasswordRequired as e:
          log.exception("RAR requires password: %s" % e)
          pass
        except rarfile.BadRarFile as e:
          log.exception("Bad RAR file")
        except rarfile.NotRarFile as e:
          log.exception("Not a RAR file")
        except rarfile.BadRarName as e:
          log.exception("Cannot guess multipart RAR name components")
        except rarfile.NoRarEntry as e:
          log.exception("File not found in RAR")
        except rarfile.NeedFirstVolume as e:
          log.exception("Need to start from first volume in RAR archive")
        except rarfile.NoCrypto as e:
          log.exception("No crypto services available to parse encrypted headers")
        except rarfile.RarExecError as e:
          log.exception("There was a problem reported by unrar")
        except rarfile.RarCRCError as e:
          log.exception("CRC error during unpacking of RAR")
        except rarfile.RarOpenError as e:
          log.exception("Error opening RAR file")
        except rarfile.RarUserError as e:
          log.exception("RAR user error")
        except rarfile.RarMemoryError as e:
          log.exception("RAR memory error")
        except rarfile.RarNoFilesError as e:
          log.exception("RAR no files that match pattern were found")
        except rarfile.RarUserBreak as e:
          log.exception("RAR user break")
        except rarfile.RarWrongPassword as e:
          log.exception("RAR wrong password")
        except rarfile.RarUnknownError as e:
          log.exception("RAR unknown error")
        except rarfile.RarCannotExec as e:
          log.exception("RAR executable not found")
        except Exception as e:
          log.exception("Unhandled exception during rar extraction")
          if self.devmode:
            log.debug("extractFilesFromMultipart(): Exiting with code 1") #we only exit if in dev mode, so we can deal with the problem afterwards
            sys.exit(1)

        #print rarFileHandle.needs_password()
        #print rarFileHandle.namelist()
        #rarFileHandle.printdir()
        #print rarFileHandle.testrar()

        #we need something here for if the entire rar file table is encrypted
        table_len = len(rarFileHandle.namelist())
        #log.debug('table_len: %s' % table_len)
        table_content = rarFileHandle.namelist()
        #log.debug('table_content: %s' % table_content)
        table_needs_password = rarFileHandle.needs_password()
        #log.debug('table_needs_password: %s' % table_needs_password)

        if table_len == 0 and table_needs_password and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0: #this means that the archive's table is encrypted and we cannot see anything inside it
          saveRarFile = True
          log.debug('extractFilesFromMultipart(): RAR archive %s has an encrypted table!' % filename)
          contentObj.contentType = 'encryptedRarTable'
          contentObj.contentFile = filename #this is the rar file itself
          contentObj.fromArchive = False # if the file is encrypted, it becomes the content itself, not what's inside it,so it's not FROM an archive, it IS the archive
          contentObj.isArchive = True
          self.thisSession['images'].append( contentObj.get() )

        for rinfo in rarFileHandle.infolist():
          contentObj = copy(origContentObj)

          archivedFilename = rinfo.filename
          contentObj.contentFile = archivedFilename
          contentObj.archiveFilename = filename
          archivedFile_is_encrypted = rinfo.needs_password()

          if archivedFile_is_encrypted and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0: #this means that the RAR file's table is not encrypted, but individual files within it are
            saveRarFile = True
            log.debug('extractFilesFromMultipart(): RAR contentFile %s from archive %s is encrypted!' % (archivedFilename,filename))
            contentObj.contentType = 'encryptedRarEntry'
            contentObj.fromArchive = True
            self.thisSession['images'].append( contentObj.get() )
            continue

          else: #identify archived file and save it permanently if a supported file type
            extractedFileObj = StringIO.StringIO() #we will write the extracted file to this buffer

            #extract the file to buffer
            compressedFileHandle = rarFileHandle.open(archivedFilename) #compressedFileHandle is a handle to the file while it's still in the rar file.  we will extract from this object
            extractedFileObj.write(compressedFileHandle.read() ) #this is where we extract the file into
            compressedFileHandle.close()

            contentObj.setStringIOContent(extractedFileObj)
            self.processExtractedFile(contentObj, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes) #now let's process the extracted file
            extractedFileObj.close()

        if saveRarFile:
          fp = open(os.path.join(self.directory, filename), 'wb')
          fp.write(part.get_payload(decode=True))
          fp.close()
        continue

    return self.thisSession



  def processExtractedFile(self, contentObj, distillationTerms, regexDistillationTerms, md5Hashes, sha1Hashes, sha256Hashes):
    log.debug("processExtractedFile(): Attempting to process extracted file " + contentObj.contentFile )
    
    #generate a new uuid for the content object
    contentObj.newId()
    
    #get the file
    fileObj = contentObj.getFileContent()

    #identify the extracted file
    fileType = magic.from_buffer( fileObj.getvalue(), mime=True) #this is where we identify the content file type

    if fileType.startswith('image/') and len(distillationTerms) == 0 and len(regexDistillationTerms) == 0:
      #log.debug("processExtractedFile(): Processing '" + archivedFilename + "' as image")
      self.processImage(contentObj)

    elif fileType == 'application/pdf':
      #log.debug("processExtractedFile(): processing '" + contentObj.contentType + "' as pdf")
      self.processPdf(contentObj, distillationTerms, regexDistillationTerms)

    elif fileType.startswith('application/'): #fix for executable
      #log.debug("processExtractedFile(): Processing '" + archivedFilename + "' as executable")
      if len(md5Hashes) != 0:
        contentObj.hashType = 'md5'
        self.genHash(contentObj, md5Hashes)
      if len(sha1Hashes) != 0:
        contentObj.hashType = 'sha1'
        self.genHash(contentObj, sha1Hashes)
      if len(sha256Hashes) != 0:
        contentObj.hashType = 'sha256'
        self.genHash(contentObj, sha256Hashes)
        
    else:
      #log.debug() "processExtractedFile(): discarding " + archivedFilename + ' with MIME type ' + fileType)
      pass

  def convertPartToStringIO(self, part):
    output = StringIO.StringIO()
    output.write(part.get_payload(decode=True))
    return output        