"""
Handle a region file, containing 32x32 chunks
For more info of the region file format look:
http://www.minecraftwiki.net/wiki/Region_file_format
"""

from .nbt import NBTFile
from struct import pack, unpack
from gzip import GzipFile
from collections import Mapping
import zlib
import gzip
from io import BytesIO
import math, time
from os.path import getsize
from os import SEEK_END

class RegionFileFormatError(Exception):
	"""Base class for all file format errors.
	Note: InconceivedChunk is not a child class, because it is not considered a format error."""
	def __init__(self, msg):
		self.msg = msg
	def __str__(self):
		return self.msg

class NoRegionHeader(RegionFileFormatError):
	"""The size of the region file is too small to contain a header."""

class RegionHeaderError(RegionFileFormatError):
	"""Error in the header of the region file for a given chunk."""

class ChunkHeaderError(RegionFileFormatError):
	"""Error in the header of a chunk, included the bytes of length and byte version."""

class ChunkDataError(RegionFileFormatError):
	"""Error in the data of a chunk."""

class InconceivedChunk(LookupError):
	"""Specified chunk has not yet been generated."""
	def __init__(self, msg):
		self.msg = msg


class _ChunkMetadata(object):
	"""
	Metadata for a particular chunk found in the 8 kiByte header and 5-byte chunk header.
	x, z: coordinates of the chunk in the file
	blockstart: start of the chunk block, counted in 4 kiByte sectors from the
	    start of the file. (24 bit int)
	blocklength: amount of 4 kiBytes sectors in the block (8 bit int)
	timestamp: a Unix timestamps (seconds since epoch) (32 bits), found in the
	    second sector in the file.
	length: length of the block in bytes. This excludes the 4-byte length header,
	    and includes the 1-byte compression byte. (32 bit int)
	compression: type of compression used for the chunk block. (8 bit int).
	- 0: uncompressed
	- 1: gzip compression
	- 2: zlib compression
	status: status as determined from blockstart, blocklength, length, file size
	    and location of other chunks in the file.
	- STATUS_CHUNK_OVERLAPPING
	- STATUS_CHUNK_MISMATCHED_LENGTHS
	- STATUS_CHUNK_ZERO_LENGTH
	- STATUS_CHUNK_IN_HEADER
	- STATUS_CHUNK_OUT_OF_FILE
	- STATUS_CHUNK_OK
	- STATUS_CHUNK_NOT_CREATED
	"""
	def __init__(self, x, z):
		self.x = x
		self.z = z
		self.blockstart = 0
		self.blocklength = 0
		self.timestamp = 0
		self.length = 0
		self.compression = None
		self.status = RegionFile.STATUS_CHUNK_NOT_CREATED
	def __str__(self):
		return "%s(%d, %d, sector=%s, length=%s, timestamp=%s, lenght=%s, compression=%s, status=%s)" % \
			(self.__class__.__name__, self.x, self.z, self.blockstart, self.blocklength, self.timestamp, \
			self.length, self.compression, self.status)
	def __repr__(self):
		return "%s(%d,%d)" % (self.__class__.__name__, self.x, self.z)
	def requiredblocks(self):
		return (self.length + 4 + 4095) // 4096
	def is_created(self):
		"""return True if this chunk is created according to the header.
		This includes chunks which are not readable for other reasons."""
		return self.blockstart != 0

class _HeaderWrapper(Mapping):
	"""Wrapper around self._header to emulate the old self.header variable"""
	def __init__(self, header):
		self.header = header
	def __getitem__(self, xz):
		m = self.header[xz]
		return (m.blockstart, m.blocklength, m.timestamp, m.status)
	def __iter__(self):
		return iter(self.header) # iterates of the keys
	def __len__(self):
		return len(self.header)
class _ChunkHeaderWrapper(Mapping):
	"""Wrapper around self._header to emulate the old self.chunk_headers variable"""
	def __init__(self, header):
		self.header = header
	def __getitem__(self, xz):
		m = self.header[xz]
		return (m.length if m.length > 0 else None, m.compression, m.status)
	def __iter__(self):
		return iter(self.header) # iterates of the keys
	def __len__(self):
		return len(self.header)

class RegionFile(object):
	"""A convenience class for extracting NBT files from the Minecraft Beta Region Format."""
	
	SECTORLEN = 4096
	"""Length of a sector; A Region file is divided in sectors of equal length."""

	# Status is a number representing:
	# -5 = Error, the chunk is overlapping with another chunk
	# -4 = Error, the chunk length is too large to fit in the sector length in the region header
	# -3 = Error, chunk header has a 0 length
	# -2 = Error, chunk inside the header of the region file
	# -1 = Error, chunk partially/completely outside of file
	#  0 = Ok
	#  1 = Chunk non-existant yet
	STATUS_CHUNK_OVERLAPPING = -5
	"""Constant indicating an error status: the chunk is allocated a sector already occupied by another chunk"""
	STATUS_CHUNK_MISMATCHED_LENGTHS = -4
	"""Constant indicating an error status: the region header length and the chunk length are incompatible"""
	STATUS_CHUNK_ZERO_LENGTH = -3
	"""Constant indicating an error status: chunk header has a 0 length"""
	STATUS_CHUNK_IN_HEADER = -2
	"""Constant indicating an error status: chunk inside the header of the region file"""
	STATUS_CHUNK_OUT_OF_FILE = -1
	"""Constant indicating an error status: chunk partially/completely outside of file"""
	STATUS_CHUNK_OK = 0
	"""Constant indicating an normal status: the chunk exists and the metadata is valid"""
	STATUS_CHUNK_NOT_CREATED = 1
	"""Constant indicating an normal status: the chunk does not exist"""
	
	COMPRESSION_NONE = 0
	"""Constant indicating tha tthe chunk is not compressed."""
	COMPRESSION_GZIP = 1
	"""Constant indicating tha tthe chunk is GZip compressed."""
	COMPRESSION_ZLIB = 2
	"""Constant indicating tha tthe chunk is zlib compressed."""
	
	def __init__(self, filename=None, fileobj=None):
		"""
		Read a region file by filename of file object. 
		If a fileobj is specified, it is not closed after use; it is the callers responibility to close that.
		"""
		self.file = None
		self.filename = None
		self._closefile = False
		if filename:
			self.filename = filename
			self.file = open(filename, 'r+b') # open for read and write in binary mode
			self._closefile = True
		elif fileobj:
			if hasattr(fileobj, 'name'):
				self.filename = fileobj.name
			self.file = fileobj
		elif not self.file:
			raise ValueError("RegionFile(): Need to specify either a filename or a file object")

		# Some variables
		self._header = {}
		"""
		dict containing _ChunkMetadata objects, gathered from metadata found in the
		8 kiByte header and 5-byte chunk header.
		"""
		self.header = _HeaderWrapper(self._header)
		"""
		dict containing the metadata found in the 8 kiByte header:
		(x,y): (offset, sectionlength, timestamp, status)
		offset counts in 4 kiByte sectors, starting from the start of the file. (24 bit int)
		blocklength is in 4 kiByte sectors (8 bit int)
		timestamp is a Unix timestamps (seconds since epoch) (32 bits)
		status is determined from offset, sectionlength and file size.
		Status can be any of:
		- STATUS_CHUNK_OVERLAPPING
		- STATUS_CHUNK_MISMATCHED_LENGTHS
		- STATUS_CHUNK_ZERO_LENGTH
		- STATUS_CHUNK_IN_HEADER
		- STATUS_CHUNK_OUT_OF_FILE
		- STATUS_CHUNK_OK
		- STATUS_CHUNK_NOT_CREATED
		"""
		self.chunk_headers = _ChunkHeaderWrapper(self._header)
		"""
		dict containing the metadata found in each chunk block:
		(x,y): (length, compression, chunk_status)
		chunk length in bytes, starting from the compression byte (32 bit int)
		compression is 1 (Gzip) or 2 (bzip) (8 bit int)
		chunk_status is equal to status in self.header.
		If the chunk is not defined, the tuple is (None, None, STATUS_CHUNK_NOT_CREATED)
		"""

		self.init_header()
		self.parse_header()
		self.parse_chunk_headers()

	def get_size(self):
		""" Returns the file object size. """
		# seek(0,2) jumps to 0-bytes from the end of the file, and returns the position
		return self.file.seek(0, SEEK_END)

	@staticmethod
	def _bytes_to_sector(bsize, sectorlen=4096):
		"""Given a size in bytes, return how many sections of length sectorlen are required to contain it.
		This is equivalent to ceil(bsize/sectorlen), if Python would use floating
		points for division, and integers for ceil(), rather than the other way around."""
		sectors, remainder = divmod(bsize, sectorlen)
		return sectors if remainder == 0 else sectors + 1
	
	def __del__(self):
		if self._closefile:
			self.file.close()
		# Parent object() has no __del__ method, otherwise it should be called here.

	def init_header(self):
		for x in range(32):
			for z in range(32):
				self._header[x,z] = _ChunkMetadata(x, z)

	def parse_header(self):
		"""Read the region header and stores: offset, length and status."""
		# update the file size, needed when parse_header is called after
		# we have unlinked a chunk or writed a new one
		self.size = self.get_size()

		if self.size == 0:
			# Some region files seems to have 0 bytes of size, and
			# Minecraft handle them without problems. Take them
			# as empty region files.
			return
		elif self.size < 8192:
			raise NoRegionHeader('The region file is too small in size to have a header.')
		
		for index in range(0, 4096, 4):
			x = int(index//4) % 32
			z = int(index//4)//32
			m = self._header[x, z]
			
			self.file.seek(index)
			offset, length = unpack(">IB", b"\0"+self.file.read(4))
			m.blockstart, m.blocklength = offset, length
			self.file.seek(index + 4096)
			m.timestamp = unpack(">I", self.file.read(4))[0]
			
			if offset == 0 and length == 0:
				m.status = RegionFile.STATUS_CHUNK_NOT_CREATED
			elif length == 0:
				m.status = RegionFile.STATUS_CHUNK_ZERO_LENGTH
			elif offset < 2 and offset != 0:
				m.status = RegionFile.STATUS_CHUNK_IN_HEADER
			elif 4096 * offset + 5 > self.size:
				# Chunk header can't be read.
				m.status = RegionFile.STATUS_CHUNK_OUT_OF_FILE
			else:
				m.status = RegionFile.STATUS_CHUNK_OK
		
		# Check for chunks overlapping in the file
		for chunks in self._sectors()[2:]:
			if len(chunks) > 1:
				# overlapping chunks
				for m in chunks:
					# Update status, unless these more severe errors take precedence
					if m.status not in (RegionFile.STATUS_CHUNK_ZERO_LENGTH, \
							RegionFile.STATUS_CHUNK_IN_HEADER, RegionFile.STATUS_CHUNK_OUT_OF_FILE):
						m.status = RegionFile.STATUS_CHUNK_OVERLAPPING

	def parse_chunk_headers(self):
		for x in range(32):
			for z in range(32):
				m = self._header[x, z]
				if m.status not in (RegionFile.STATUS_CHUNK_OK, RegionFile.STATUS_CHUNK_OVERLAPPING, \
						RegionFile.STATUS_CHUNK_MISMATCHED_LENGTHS):
					continue
				try:
					self.file.seek(m.blockstart*4096) # offset comes in sectors of 4096 bytes
					length = unpack(">I", self.file.read(4))
					m.length = length[0] # unpack always returns a tuple, even unpacking one element
					compression = unpack(">B",self.file.read(1))
					m.compression = compression[0]
				except IOError:
					m.status = RegionFile.STATUS_CHUNK_OUT_OF_FILE
					continue
				if m.length <= 1: # chunk can't be zero length
					m.status = RegionFile.STATUS_CHUNK_ZERO_LENGTH
				elif m.length + 4 > m.blocklength * 4096:
					# There are not enough sectors allocated for the whole block
					m.status = RegionFile.STATUS_CHUNK_MISMATCHED_LENGTHS

	def _sectors(self, ignore_chunk=None):
		"""
		Return a list of all sectors, each sector is a list of chunks occupying the block.
		"""
		sectorsize = self._bytes_to_sector(self.size)
		sectors = [[] for s in range(sectorsize)]
		sectors[0] = None # locations
		sectors[1] = None # timestamps
		for m in self._header.values():
			if not m.is_created():
				continue
			if ignore_chunk == m:
				continue
			if m.blocklength and m.blockstart:
				for b in range(m.blockstart, m.blockstart + max(m.blocklength, m.requiredblocks())):
					if 2 <= b < sectorsize:
						sectors[b].append(m)
		return sectors

	def _locate_free_sectors(self, ignore_chunk=None):
		"""Return a list of booleans, indicating the free sectors."""
		sectors = self._sectors(ignore_chunk=ignore_chunk)
		return [i != None and len(i) == 0 for i in sectors] #+ required_sectors*[True]

	def _find_free_location(self, free_locations, required_sectors=1, preferred=None):
		"""
		Given a list of booleans, find a list of <required_sectors> consecutive True values.
		If no such list is found, return length(free_locations).
		Assumes first two values are always False.
		"""
		# check preferred (current) location
		if preferred and all(free_locations[preferred:preferred+required_sectors]):
			return preferred
		
		# check other locations
		i = 2 # First two sectors are in use by the header
		while i < len(free_locations):
			if all(free_locations[i:i+required_sectors]):
				break
			i += 1
		return i
		# TODO: test codes with the following algorithm as well. (this starts searching free blocks at the end of the file.)
		# i = len(free_locations) - required_sectors
		# while i >= 2:
		# 	if all(free_locations[i:i+required_sectors]):
		# 		return i
		# 	i -= 1
		# return len(free_locations)

	def get_chunk_metadata(self):
		"""
		Return the metadata of each chunk that is defined in te regionfile.
		This includes chunks which may not be readable for whatever reason,
		but excludes chunks that are not yet defined.
		"""
		return [m for m in self._header.values() if m.is_created()]

	def get_chunks(self):
		"""
		Return coordinates and length of all chunks.

		Warning: despite the name, this function does not actually return the chunk,
		but merely it's metadata. Use get_chunk(x,z) to get the NBTFile, and then Chunk()
		to get the actual chunk.
		
		This method is deprecated. Use get_chunk_metadata() instead.
		"""
		return self.get_chunk_coords()

	def get_chunk_coords(self):
		"""
		Return the x,z coordinates and length of the chunks that are defined in te regionfile.
		This includes chunks which may not be readable for whatever reason.
		
		This method is deprecated. Use get_chunk_metadata() instead.
		"""
		chunks = []
		for x in range(32):
			for z in range(32):
				length = self.chunk_headers[x,z]
				if self.header[x,z][0] > 0:
					chunks.append({'x': x, 'z': z, 'length': length})
		return chunks

	def iter_chunks(self):
		"""
		Yield each readable chunk present in the region.
		Chunks that can not be read for whatever reason are silently skipped.
		Warning: this function returns a NBTFile() object, use Chunk(nbtfile) to get a
		Chunk instance.
		"""
		for m in self.get_chunk_metadata():
			try:
				yield self.get_chunk(m.x, m.z)
			except RegionFileFormatError:
				pass
	
	def __iter__(self):
		return self.iter_chunks()

	def get_timestamp(self, x, z):
		"""Return the timestamp of when this region file was last modified."""
		# TODO: raise an exception if chunk does not exist?
		# TODO: return a datetime.datetime object using datetime.fromtimestamp()
		return self.header[x,z][2]

	def chunk_count(self):
		"""Return the number of defined chunks. This includes potentially corrupt chunks."""
		return len(self.get_chunk_metadata())

	def get_nbt(self, x, z):
		"""Return a NBTFile"""
		return self.get_chunk(x, z)

	def get_chunk(self, x, z):
		"""Return a NBTFile"""
		# read metadata block
		# TODO: deprecate in favour of get_nbt?
		m = self._header[x, z]
		offset, length, timestamp, region_header_status = self.header[x, z]
		if m.status == RegionFile.STATUS_CHUNK_NOT_CREATED:
			raise InconceivedChunk("Chunk is not created")
		elif m.status == RegionFile.STATUS_CHUNK_IN_HEADER:
			raise RegionHeaderError('Chunk %d,%d is in the region header' % (x,z))
		elif region_header_status == RegionFile.STATUS_CHUNK_OUT_OF_FILE:
			raise RegionHeaderError('Chunk %d,%d is partially/completely outside the file' % (x,z))
		elif m.status == RegionFile.STATUS_CHUNK_ZERO_LENGTH:
			if m.blocklength == 0:
				raise RegionHeaderError('Chunk %d,%d has zero length' % (x,z))
			else:
				raise ChunkHeaderError('Chunk %d,%d has zero length' % (x,z))

		# status is STATUS_CHUNK_OK, STATUS_CHUNK_MISMATCHED_LENGTHS or STATUS_CHUNK_OVERLAPPING.
		# The chunk is always read, but in case of an error, the exception may be different 
		# based on the status.

		# offset comes in sectors of 4096 bytes + length bytes + compression byte
		self.file.seek(m.blockstart * 4096 + 5)
		chunk = self.file.read(m.length-1) # the length in the file includes the compression byte

		if m.compression == None:
			print(m)
			print(self.header[m])
		
		err = None
		if m.compression > 2:
			raise ChunkDataError('Unknown chunk compression/format (%d)' % m.compression)
		try:
			if (m.compression == RegionFile.COMPRESSION_GZIP):
				chunk = gzip.decompress(chunk)
			elif (m.compression == RegionFile.COMPRESSION_ZLIB):
				chunk = zlib.decompress(chunk)
			chunk = BytesIO(chunk)
			return NBTFile(buffer=chunk) # this may raise a MalformedFileError.
		except Exception as e:
			# Deliberately catch the Exception and re-raise.
			# The details in gzip/zlib/nbt are irrelevant, just that the data is garbled.
			err = str(e)
		if err:
			# don't raise during exception handling to avoid the warning 
			# "During handling of the above exception, another exception occurred".
			# Python 3.3 solution (see PEP 409 & 415): "raise ChunkDataError(str(e)) from None"
			if m.status == RegionFile.STATUS_CHUNK_MISMATCHED_LENGTHS:
				raise ChunkHeaderError('The length in region header and the length in the header of chunk %d,%d are incompatible' % (x,z))
			elif m.status == RegionFile.STATUS_CHUNK_OVERLAPPING:
				raise ChunkHeaderError('Chunk %d,%d is overlapping with another chunk' % (x,z))
			else:
				raise ChunkDataError(err)

	def write_chunk(self, x, z, nbt_file):
		""" A simple chunk writer. """
		data = BytesIO()
		nbt_file.write_file(buffer = data) # render to buffer; uncompressed

		compressed = zlib.compress(data.getvalue()) # use zlib compression, rather than Gzip
		data = BytesIO(compressed)
		length = len(data.getvalue())

		# 5 extra bytes are required for the chunk block header
		nsectors = self._bytes_to_sector(length + 5)

		if nsectors >= 256:
			raise ChunkDataError("Chunk is too large (%d sectors exceeds 255 maximum)" % (nsectors))

		# search for a place where to write the chunk:
		current = self._header[x, z]
		free_sectors = self._locate_free_sectors(ignore_chunk=current)
		sector = self._find_free_location(free_sectors, nsectors, preferred = current.blockstart)

		# write out chunk to region
		self.file.seek(sector*4096)
		self.file.write(pack(">I", length + 1)) #length field
		self.file.write(pack(">B", RegionFile.COMPRESSION_ZLIB)) #compression field
		self.file.write(data.getvalue()) #compressed data

		# Write zeros up to the end of the chunk
		remaininglength = 4096 * nsectors - length - 5
		self.file.write(remaininglength * b"\x00")

		#seek to header record and write offset and length records
		self.file.seek(4*(x+z*32))
		self.file.write(pack(">IB", sector, nsectors)[1:])

		#write timestamp
		self.file.seek(4096+4*(x+z*32))
		timestamp = int(time.time())
		self.file.write(pack(">I", timestamp))

		# Update free_sectors with newly written block
		# This is required for calculating file truncation and zeroing freed blocks.
		free_sectors.extend((sector + nsectors - len(free_sectors)) * [True])
		for s in range(sector, sector + nsectors):
			free_sectors[s] = False
		
		# Check if file should be truncated:
		truncate_count = list(reversed(free_sectors)).index(False)
		if truncate_count > 0:
			self.size = 4096 * (len(free_sectors) - truncate_count)
			self.file.truncate(self.size)
			free_sectors = free_sectors[:-truncate_count]
		
		# Calculate freed sectors
		for s in range(current.blockstart, min(current.blockstart + current.blocklength, len(free_sectors))):
			if free_sectors[s]:
				# zero sector s
				self.file.seek(4096*s)
				self.file.write(4096*b'\x00')
		
		# update file size and header information
		self.size = self.get_size()
		current.blockstart = sector
		current.blocklength = nsectors
		current.status = RegionFile.STATUS_CHUNK_OK
		current.timestamp = timestamp
		current.length = length + 1
		current.compression = RegionFile.COMPRESSION_ZLIB

		# self.parse_header()
		# self.parse_chunk_headers()


	def unlink_chunk(self, x, z):
		"""
		Remove a chunk from the header of the region file (write zeros
		in the offset of the chunk). Fragmentation is not a problem,
		Minecraft and this nbt library write chunks in old free spaces
		when possible.
		"""
		# This function fails for an empty file. If that is the case, just return.
		if self.size < 8192:
			return

		# zero the region header for the chunk (offset length and time)
		self.file.seek(4*(x+z*32))
		self.file.write(pack(">IB", 0, 0)[1:])
		self.file.seek(4096+4*(x+z*32))
		self.file.write(pack(">I", 0))

		# TODO: zero cleared chunks, provided that they are in the file and non-overlapping.
		
		# TODO: truncate file if possible.
		
		# update the header
		self._header[x, z] = _ChunkMetadata(x, z)

	def _classname(self):
		"""Return the fully qualified class name."""
		if self.__class__.__module__ in (None,):
			return self.__class__.__name__
		else:
			return "%s.%s" % (self.__class__.__module__, self.__class__.__name__)

	def __str__(self):
		if self.filename:
			return "<%s(%r)>" % (self._classname(), self.filename)
		else:
			return '<%s object at %d>' % (self._classname(), id(self))
	
	def __repr__(self):
		if self.filename:
			return "%s(%r)" % (self._classname(), self.filename)
		else:
			return '<%s object at %d>' % (self._classname(), id(self))
