import logging
import email
from io import BytesIO
from uuid import uuid4

log = logging.getLogger(__name__)

class ContentObj:

    def __init__(self, session=0, contentType='', contentSubType='', contentFile='', proxyContentFile='', image='', thumbnail='', hashType='', hashValue='', hashFriendly='', fromArchive=False, archiveType='', archiveFilename='', isArchive=False, textDistillationEnabled=False, regexDistillationEnabled=False, textTermsMatched=[], regexTermsMatched=[]):
        self.session = session                   # number: session id
        self.contentType = contentType           # should be image, pdf, office, or hash, unsupportedZipEntry, encryptedZipEntry, encryptedRarEntry, encryptedRarTable
        self.contentSubType = contentSubType     # optional: either word, excel, or powerpoint

        self.contentFile = contentFile           # the image or office or pdf or exe filename
        self.proxyContentFile = proxyContentFile # optional, this is a pdf document which we may substitute for a converted original office doc.  This will be rendered by the client instead
        self.pdfImage = image                    # the PDF gs-generated image filename
        self.thumbnail = thumbnail               # thumbnail image file - only used for images, not pdf's or office
        self.archiveFilename = archiveFilename   # the name of the zip or rar archive

        # Hash
        self.hashType = hashType                  # sha1, sha256, md5
        self.hashValue = hashValue               
        self.hashFriendly = hashFriendly         # friendly name of hash, if there is one
        
        #Archives
        self.fromArchive = fromArchive           # boolean, whether the content file came from a zip or rar archive
        self.isArchive = isArchive               # boolean, whether the file IS an archive rather than came from an archive
        self.archiveType = archiveType           # either zip or rar

        #Distillation
        self.textDistillationEnabled = textDistillationEnabled
        self.regexDistillationEnabled = regexDistillationEnabled
        self.textTermsMatched = textTermsMatched
        self.regexTermsMatched = regexTermsMatched

        self.fileContent = None

        self.id = str(uuid4()) # generate a unique identifier for this content
        #log.debug('ContentObj: __init__(): generated new id: ' + self.id)

    def newId(self):
        self.id = str(uuid4())
        #log.debug('ContentObj: newId(): generated new id: ' + self.id)
        

    def get(self):
        o = {}
        o['session'] = self.session
        if self.contentType:
            o['contentType'] = self.contentType
        if self.contentSubType:
            o['contentSubType'] = self.contentSubType
        if self.contentFile:
            o['contentFile'] = self.contentFile
        if self.proxyContentFile:
            o['proxyContentFile'] = self.proxyContentFile
        if self.pdfImage:
            o['pdfImage'] = self.pdfImage
        if self.thumbnail:
            o['thumbnail'] = self.thumbnail
        if self.hashType:
            o['hashType'] = self.hashType
        if self.hashValue:
            o['hashValue'] = self.hashValue
        if self.hashFriendly:
            o['hashFriendly'] = self.hashFriendly
        o['fromArchive'] = self.fromArchive
        o['isArchive'] = self.isArchive
        if self.archiveType:
            o['archiveType'] = self.archiveType
        if self.archiveFilename:
            o['archiveFilename'] = self.archiveFilename
        
        o['textDistillationEnabled'] = self.textDistillationEnabled
        o['regexDistillationEnabled'] = self.regexDistillationEnabled
        if len(self.textTermsMatched) != 0:
            o['textTermsMatched'] = self.textTermsMatched
        if len(self.regexTermsMatched) != 0:
            o['regexTermsMatched'] = self.regexTermsMatched
        o['id'] = self.id
        return o

    def setPartContent(self, content): # takes an email part object
        #self.fileContent = cStringIO.StringIO()
        self.fileContent = BytesIO()
        self.fileContent.write(content.get_payload(decode=True))

    def setStringIOContent(self, content): #takes a BytesIO object
        self.fileContent = content


    def getFileContent(self):
        self.fileContent.seek(0)
        return self.fileContent
        #return self.fileContent.getvalue()

    def getCopy(self):
        # returns an ContentObj that should be a near copy of this object, excepting id
        obj = ContentObj(self.session, self.contentType, self.contentSubType, self.contentFile, self.proxyContentFile, self.pdfImage, self.thumbnail, self.hashType, self.hashValue, self.hashFriendly, self.fromArchive, self.archiveType, self.archiveFilename, self.isArchive, self.textDistillationEnabled, self.regexDistillationEnabled, self.textTermsMatched, self.regexTermsMatched)
        if self.fileContent:
            obj.setStringIOContent(self.getFileContent())
        return obj
