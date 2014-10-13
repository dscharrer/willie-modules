# -*- coding: utf-8 -*-
"""
pipe.py - Willie Module that reads from a UNIX named pipe and outputs lines to IRC
Copyright © 2014, Daniel Scharrer, <daniel@constexpr.org>
Licensed under the Eiffel Forum License 2.

Reads lines according to the following format:
 recipient message
"""

import os
import time
import socket
import codecs
import threading
import traceback
from copy import copy
from willie.module import event, rule, priority
from willie.config import ConfigurationError


class Pipe:
	
	
	def __init__(self, bot):
		self.bot = bot
		self.name = '(default)'
		self.file = None
		self.socket_file = None
		self.buffer_file = None
		self.exclude = set()
		self.enable = set()
		self.thread = None
		self.running = True
		self.listen_timeout = 5 * 60
	
	
	def parse_config(self, section):
		
		if section.file:
			self.file = section.file
			self.socket_file = self.file + '/socket'
			self.buffer_file = self.file + '/buffer'
		
		exclude = section.get_list('exclude')
		if exclude:
			self.exclude = set(exclude)
		
		enable = section.get_list('enable')
		if enable:
			self.enable = set(enable)
	
	
	def validate_config(self):
		
		if not self.socket_file or not self.buffer_file:
			raise ConfigurationError('Missing pipe file for pipe {0}'.format(self.name))
	
	
	def warn(self, message):
		self.bot.debug(__file__, u'Pipe {0}: {1}'.format(self.name, message), 'warning')
	
	
	def process_line(self, line):
		
		(recipient, message) = line.split(' ', 1)
		
		# Normalize input
		recipient = recipient.lower()
		message = message.strip()
		
		# Apply whitelist, if present
		if self.enable and recipient not in self.enable:
			self.warn(u'{0} is not whitelisted'.format(recipient))
			return
		# Apply blacklist, if present
		if recipient in self.exclude:
			self.warn(u'{0} is blacklisted'.format(recipient))
			return
		
		try:
			self.bot.msg(recipient, message, 5)
		except Exception as e:
			self.warn(u'error sending message: {0}'.format(traceback.format_exc(e)))
			try:
				buffer = codecs.open(self.buffer_file, 'a', encoding='utf-8')
			except Exception as e:
				self.warn(u'error opening buffer file for writing {0}: {1}'.format(
					self.buffer_file, traceback.format_exc(e)))
			try:
				os.chmod(self.buffer_file, 0666)
				buffer.write(line + '\n');
			finally:
				buffer.close()
	
	
	def process_lines(self, stream, send = True):
		
		buffer = None
		if not self.running:
			try:
				buffer = codecs.open(self.buffer_file, 'a', encoding='utf-8')
				os.chmod(self.buffer_file, 0666)
			except Exception as e:
				self.warn(u'error opening buffer file for writing {0}: {1}'.format(
					self.buffer_file, traceback.format_exc(e)))
		
		try:
			
			for line in stream:
				try:
					line = line.rstrip()
					if line:
						if buffer:
							buffer.write(line + '\n');
						else:
							self.process_line(line)
				except Exception as e:
					line = "".join(i if ord(i) < 128 else '?' for i in line)
					self.warn(u'bad line "{0}": {1}'.format(line, traceback.format_exc(e)))
			
		finally:
			if buffer:
				buffer.close();
	
	
	def handle_connection(self, handle):
		try:
			connection, client_address = handle.accept()
			try:
				self.process_lines(connection.makefile())
			except Exception as e:
				self.warn(u'error reading from connection on socket {0}: {1}'.format(
					self.socket_file, traceback.format_exc(e)))
			finally:
				connection.close()
		except:
			return False
		return True
	
	
	def flush_connections(self, handle):
		handle.settimeout(0)
		while True:
			if not self.handle_connection(handle):
				break
	
	
	def clean(self):
		if os.path.exists(self.socket_file):
			try:
				os.unlink(self.socket_file)
			except Exception as e:
				self.warn(u'cant remove stale socket file {0}: {1}'.format(
					self.socket_file, traceback.format_exc(e)))
				return
	
	
	def run(self):
		
		time.sleep(1)
		
		# Remove existing socket files
		self.clean()
		
		# Start listening on the unix domain socket
		handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		handle.bind(self.socket_file)
		handle.listen(5)
		try:
			os.chmod(self.socket_file, 0666)
			
			first = True
			while self.running:
				if first:
					first = False
				
				# Proecess lines buffered while the socket was down
				if os.path.exists(self.buffer_file):
					try:
						buffer = open(self.buffer_file, 'r')
						os.unlink(self.buffer_file)
						if not first:
							self.flush_connections(handle)
						self.process_lines(buffer)
					except Exception as e:
						self.warn(u'error reading buffer file {0}: {1}'.format(
							self.buffer_file, traceback.format_exc(e)))
						pass
				
				# Wait for a connection
				handle.settimeout(self.listen_timeout)
				self.handle_connection(handle)
			
			# Stop accepting new connections (as best as possible)
			self.clean()
			handle.listen(0)
			
			# Process any existing connections before exitin
			self.flush_connections(handle)
			
		finally:
			handle.close()
	
	
	def start(self):
		if self.thread is not None:
			return
		self.thread = threading.Thread(target=self.run)
		self.thread.daemon = True
		self.thread.start()
	
	def stop(self):
		if self.thread is None:
			return
		# Signal the thread to exit
		self.running = False
		handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		handle.connect(self.socket_file)
		handle.close()
		# Wait for the thread to exit
		self.thread.join()


def setup(bot):
	
	if not bot.config.has_section('pipe'):
		raise ConfigurationError('Missing pipe config section')
	
	defaults = Pipe(bot)
	defaults.parse_config(bot.config.pipe)
	
	pipes = [ ]
	
	names = bot.config.pipe.get_list('pipes')
	if not names:
		pipes.append(defaults)
	else:
		for name in names:
			
			section = 'pipe_' + name
			if not bot.config.has_section(section):
				raise ConfigurationError('Missing pipe config section for pipe {0}'.format(name))
			
			pipe = copy(defaults)
			pipe.name = name
			pipe.parse_config(getattr(bot.config, section))
			
			pipes.append(pipe)
	
	for pipe in pipes:
		pipe.validate_config()
	
	bot.memory['pipes_started'] = False
	bot.memory['pipes'] = pipes

@event('JOIN')
@rule(r'.*')
@priority('low')
def connected(bot, trigger):
	
	if bot.memory['pipes_started']:
		return
	bot.memory['pipes_started'] = True
	
	for pipe in bot.memory['pipes']:
		pipe.start()
		bot.debug(__file__, 'Pipe {0}: {1} on +{2} -{3}'.format(pipe.name,
			pipe.file, ' +'.join(pipe.enable), ' -'.join(pipe.exclude)), 'warning')

def shutdown(bot):
	
	for pipe in bot.memory['pipes']:
		pipe.stop()
	
	bot.memory['pipes'] = None
