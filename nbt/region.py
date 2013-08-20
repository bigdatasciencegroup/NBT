"""
Handle a region file, containing 32x32 chunks
For more info of the region file format look:
http://www.minecraftwiki.net/wiki/Region_file_format
"""

from .nbt import NBTFile
from struct import pack, unpack
from gzip import GzipFile
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

		self.header = {}
		"""
		dict containing the metadata found in the 8 kiByte header:
		(x,y): (offset, sectionlength, timestamp, status)
		offset counts in 4 kiByte sectors, starting from the start of the file. (24 bit int)
		blocklength is in 4 kiByte sectors (8 bit int)
		timestamp is a Unix timestamps (seconds since epoch) (32 bits)
		status is determined from offset, sectionlength and file size.
		Status can be any of:
		- STATUS_CHUNK_MISMATCHED_LENGTHS
		- STATUS_CHUNK_IN_HEADER
		- STATUS_CHUNK_OUT_OF_FILE
		- STATUS_CHUNK_OK
		- STATUS_CHUNK_NOT_CREATED
		"""
		self.chunk_headers = {}
		"""
		dict containing the metadata found in each chunk block:
		(x,y): (length, compression, chunk_status)
		chunk length in bytes, starting from the compression byte (32 bit int)
		compression is 1 (Gzip) or 2 (bzip) (8 bit int)
		chunk_status is determined from sectionlength and status (as found in the header).
		chunk_status can be any of:
		- STATUS_CHUNK_MISMATCHED_LENGTHS (status will be STATUS_CHUNK_OK or STATUS_CHUNK_MISMATCHED_LENGTHS)
		- STATUS_CHUNK_ZERO_LENGTH (status will be STATUS_CHUNK_OK)
		- STATUS_CHUNK_IN_HEADER
		- STATUS_CHUNK_OUT_OF_FILE
		- STATUS_CHUNK_OK
		- STATUS_CHUNK_NOT_CREATED
		If the chunk is not defined, the tuple is (None, None, STATUS_CHUNK_NOT_CREATED)
		"""
		
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
				self.header[x,z] = (0, 0, 0, self.STATUS_CHUNK_NOT_CREATED)

	def parse_header(self):
		"""Read the region header and stores: offset, length and status."""
		# update the file size, needed when parse_header is called after
		# we have unlinked a chunk or writed a new one
		self.size = self.get_size()

		if self.size == 0:
			# Some region files seems to have 0 bytes of size, and
			# Minecraft handle them without problems. Take them
			# as empty region files.
			self.init_header()
			return
		elif self.size < 8192:
			self.init_header()
			raise NoRegionHeader('The region file is too small in size to have a header.')
		
		for index in range(0,4096,4):
			self.file.seek(index)
			offset, length = unpack(">IB", b"\0"+self.file.read(4))
			self.file.seek(index + 4096)
			timestamp = unpack(">I", self.file.read(4))[0]
			x = int(index//4) % 32
			z = int(index//4)//32
			if offset == 0 and length == 0:
				status = self.STATUS_CHUNK_NOT_CREATED

			elif length == 0:
				status = self.STATUS_CHUNK_MISMATCHED_LENGTHS

			elif offset < 2 and offset != 0:
				status = self.STATUS_CHUNK_IN_HEADER
			
			# TODO: check for chunks overlapping in file
			# make list of files sectors -> chunk (in addition to the chunk -> sectors dict)?

			# (don't forget!) offset and length comes in sectors of 4096 bytes
			#TODO: would it be allowed if self.size would be a bit smaller, because the last bytes are not zeroed, 
			# or MUST the file size be a multiple of 4096?
			elif (offset + length)*4096 > self.size:
				status = self.STATUS_CHUNK_OUT_OF_FILE

			else:
				status = self.STATUS_CHUNK_OK

			self.header[x,z] = (offset, length, timestamp, status)

	def parse_chunk_headers(self):
		for x in range(32):
			for z in range(32):
				offset, region_header_length, timestamp, status = self.header[x,z]

				if status == self.STATUS_CHUNK_NOT_CREATED:
					length = None
					compression = None
					chunk_status = self.STATUS_CHUNK_NOT_CREATED

				elif status == self.STATUS_CHUNK_OK:
					self.file.seek(offset*4096) # offset comes in sectors of 4096 bytes
					length = unpack(">I", self.file.read(4))
					length = length[0] # unpack always returns a tuple, even unpacking one element
					compression = unpack(">B",self.file.read(1))
					compression = compression[0]
					if length == 0: # chunk can't be zero length
						chunk_status = self.STATUS_CHUNK_ZERO_LENGTH
					elif length + 4 > region_header_length*4096: # Add 4 length bytes in the chunk header
						# the lengths stored in region header and chunk
						# header are not compatible
						chunk_status = self.STATUS_CHUNK_MISMATCHED_LENGTHS
					else:
						chunk_status = self.STATUS_CHUNK_OK

				elif status == self.STATUS_CHUNK_OUT_OF_FILE:
					if offset*4096 + 5 < self.size: # if possible read it, just in case it's useful
						self.file.seek(offset*4096) # offset comes in sectors of 4096 bytes
						length = unpack(">I", self.file.read(4))
						length = length[0] # unpack always returns a tuple, even unpacking one element
						compression = unpack(">B",self.file.read(1))
						compression = compression[0]
						chunk_status = self.STATUS_CHUNK_OUT_OF_FILE

					else:
						length = None
						compression = None
						chunk_status = self.STATUS_CHUNK_OUT_OF_FILE

				else:
					length = None
					compression = None
					chunk_status = status

				self.chunk_headers[x, z] = (length, compression, chunk_status)


	def locate_free_space(self, required_sectors):
		pass

	def get_chunks(self):
		"""
		Return coordinates and length of all chunks.

		Warning: despite the name, this function does not actually return the chunk,
		but merely it's metadata. Use get_chunk(x,z) to get the NBTFile, and then Chunk()
		to get the actual chunk.
		"""
		return self.get_chunk_coords()

	def get_chunk_coords(self):
		"""
		Return the x,z coordinates and length of the chunks that are defined in te regionfile.
		This includes chunks which may not be readable for whatever reason.
		"""
		# TODO: deprecate this function, and replace with one that returns objects instead of a dict, and has a better name (get_chunk_metadata(), get_metadata()?)
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
		# TODO: allow iteration over RegionFile self. (thus: for chunk in RegionFile('region.mcr'): ... )
		for cc in self.get_chunk_coords():
			try:
				yield self.get_chunk(cc['x'], cc['z'])
			except RegionFileFormatError:
				pass

	def get_timestamp(self, x, z):
		"""Return the timestamp of when this region file was last modified."""
		# TODO: raise an exception if chunk does not exist?
		# TODO: return a datetime.datetime object using datetime.fromtimestamp()
		return self.header[x,z][2]

	def chunk_count(self):
		"""Return the number of defined chunks. This includes potentially corrupt chunks."""
		return len(self.get_chunk_coords())

	def get_nbt(self, x, z):
		"""Return a NBTFile"""
		return self.get_chunk(x, z)

	def get_chunk(self, x, z):
		"""Return a NBTFile"""
		#read metadata block
		# TODO: deprecate in favour of get_nbt?
		offset, length, timestamp, region_header_status = self.header[x, z]
		if region_header_status == self.STATUS_CHUNK_NOT_CREATED:
			raise InconceivedChunk("Chunk is not created")

		elif region_header_status == self.STATUS_CHUNK_IN_HEADER:
			raise RegionHeaderError('Chunk %d,%d is in the region header' % (x,z))

		elif region_header_status == self.STATUS_CHUNK_MISMATCHED_LENGTHS:
			raise RegionHeaderError('Chunk %d,%d is has zero length' % (x,z))

		elif region_header_status == self.STATUS_CHUNK_OUT_OF_FILE:
			raise RegionHeaderError('Chunk %d,%d is partially/completely outside the file' % (x,z))

		elif region_header_status == self.STATUS_CHUNK_OK:
			length, compression, chunk_header_status = self.chunk_headers[x, z]
			if chunk_header_status == self.STATUS_CHUNK_ZERO_LENGTH:
				raise ChunkHeaderError('The length of chunk %d,%d is 0 sectors' % (x,z))
			elif chunk_header_status == self.STATUS_CHUNK_MISMATCHED_LENGTHS:
				# Attempt to read it anyway. (TODO: use logging module to log warning)
				pass
				# raise ChunkHeaderError('The length in region header and the length in the header of chunk %d,%d are incompatible' % (x,z))

			if (length == None) or (length <= 1):
				raise ChunkHeaderError("Chunk length is 0 bytes")

			self.file.seek(offset*4096 + 5) # offset comes in sectors of 4096 bytes + length bytes + compression byte
			chunk = self.file.read(length-1) # the length in the file includes the compression byte

			err = None
			if compression > 2:
				raise ChunkDataError('Unknown chunk compression/format (%d)' % compression)
			try:
				if (compression == 1):
					chunk = gzip.decompress(chunk)
				elif (compression == 2):
					chunk = zlib.decompress(chunk)
				chunk = BytesIO(chunk)
				return NBTFile(buffer=chunk) # this may raise a MalformedFileError.
			except Exception as e:
				# Deliberately catch the Exception and re-raise.
				# The details in gzip/zlib/nbt are irrelevant, just that the data is garbled.
				err = str(e)
			finally:
				if err:
					# don't raise during exception handling to avoid the warning 
					# "During handling of the above exception, another exception occurred".
					# Python 3.3 solution (see PEP 409 & 415): "raise ChunkDataError(str(e)) from None"
					raise ChunkDataError(err)
		else:
			raise RegionHeaderError("Chunk has unknown status")

	def write_chunk(self, x, z, nbt_file):
		""" A simple chunk writer. """
		data = BytesIO()
		nbt_file.write_file(buffer = data) #render to buffer; uncompressed

		compressed = zlib.compress(data.getvalue()) #use zlib compression, rather than Gzip
		data = BytesIO(compressed)

		# 5 extra bytes are required for the chunk block header
		nsectors = self._bytes_to_sector(len(data.getvalue()) + 5)
		# TODO: raise error if nsectors > 256 (because the length byte can no longer fit in the 1-byte length field in the header)

		# search for a place where to write the chunk:
		offset, length, timestamp, status = self.header[x, z]
		pad_end = False

		if status in (self.STATUS_CHUNK_NOT_CREATED, self.STATUS_CHUNK_OK):
			# look up if the new chunk fits in the place of the old one,
			# a no created chunk has 0 length, so can't be a problem
			if nsectors <= length:
				sector = offset
			else:
				# let's find a free place for this chunk
				found = False
				# sort the chunk tuples by offset and ignore empty chunks
				l = sorted([i for i in self.header.values() if i[0] != 0])

				# TODO: What is l[0][0]?
				if l[0][0] != 2:
					# there is space between the header and the first
					# used sector, insert a false tuple to check that
					# space too
					l.insert(0,(2,0,0,0))

				# iterate chunks by offset and search free space
				for i in range(len(l) - 1):
					# first item in the tuple is offset, second length
					
					current_chunk = l[i]
					next_chunk = l[i+1]
					# calculate free_space beween chunks and break if enough
					free_space = next_chunk[0] - (current_chunk[0] + current_chunk[1])
					if free_space >= nsectors:
						sector = current_chunk[0] + current_chunk[1]
						# a corrupted region header can contain random
						# stuff, just in case check if we are trying to
						# write in the header and skip if it's the case.
						if sector <= 1:
							continue
						found  = True
						break

				if not found: # append chunk to the end of the file
					self.file.seek(0, SEEK_END) # go to the end of the file
					file_length = self.file.tell()-1 # current offset is file length
					total_sectors = self._bytes_to_sector(file_length)
					sector = total_sectors+1
					pad_end = True
		else:
			# status is (self.STATUS_CHUNK_OUT_OF_FILE, self.STATUS_CHUNK_IN_HEADER,
			#      self.STATUS_CHUNK_ZERO_LENGTH, self.STATUS_CHUNK_MISMATCHED_LENGTHS)
			# don't trust bad headers, this chunk hasn't been generated yet, or the header is wrong
			# This chunk should just be appended to the end of the file
			self.file.seek(0, SEEK_END) # go to the end of the file
			file_length = self.file.tell()-1 # current offset is file length
			total_sectors = self._bytes_to_sector(file_length)
			sector = total_sectors+1
			pad_end = True


		# write out chunk to region
		self.file.seek(sector*4096)
		self.file.write(pack(">I", len(data.getvalue())+1)) #length field
		self.file.write(pack(">B", 2)) #compression field
		self.file.write(data.getvalue()) #compressed data
		if pad_end:
			# Write zeros up to the end of the chunk
			self.file.seek((sector+nsectors)*4096-1)
			# TODO: this would write only one zero-byte, shouldn't this be more bytes long?
			self.file.write(b"\x00")

		#seek to header record and write offset and length records
		self.file.seek(4*(x+z*32))
		self.file.write(pack(">IB", sector, nsectors)[1:])

		#write timestamp
		self.file.seek(4096+4*(x+z*32))
		timestamp = int(time.time())
		self.file.write(pack(">I", timestamp))

		# update file size and header information
		self.parse_header()
		self.parse_chunk_headers()


	def unlink_chunk(self, x, z):
		"""
		Remove a chunk from the header of the region file (write zeros
		in the offset of the chunk). Fragmentation is not a problem,
		Minecraft and this nbt library write chunks in old free spaces
		when possible.
		"""
		# TODO: this function fails for an empty file. If that is the case, just return.

		# zero the region header for the chunk (offset length and time)
		self.file.seek(4*(x+z*32))
		self.file.write(pack(">IB", 0, 0)[1:])
		self.file.seek(4096+4*(x+z*32))
		self.file.write(pack(">I", 0))

		# update the header
		self.parse_header()
		self.parse_chunk_headers()

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
