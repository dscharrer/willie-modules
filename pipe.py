# -*- coding: utf-8 -*-
"""
pipe.py - Willie Module that reads from a UNIX named pipe and outputs lines to IRC
Copyright © 2014, Daniel Scharrer, <daniel@constexpr.org>
Licensed under the Eiffel Forum License 2.

Reads lines according to the following format:
 recipient message
"""

import os
import codecs
import threading
import traceback
from copy import copy
from willie.config import ConfigurationError


DEBUG = 'verbose'


class Pipe:
	
	
	def __init__(self, bot):
		self.bot = bot
		self.name = '(default)'
		self.file = None
		self.exclude = set()
		self.enable = set()
		self.thread = None
		self.running = True
	
	
	def parse_config(self, section):
		
		if section.file:
			self.file = section.file
		
		exclude = section.get_list('exclude')
		if exclude:
			self.exclude = set(exclude)
		
		enable = section.get_list('enable')
		if enable:
			self.enable = set(enable)
	
	
	def validate_config(self):
		
		if not self.file:
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
		
		self.bot.msg(recipient, message, 5)
	
	def run(self):
		
		try:
			if not os.path.exists(self.file):
				os.mkfifo(self.file)
		except Exception as e:
			self.warn(u'cant create pipe file {0}: {1}'.format(self.file, traceback.format_exc(e)))
			return
		
		while self.running:
			
			try:
				handle = codecs.open(self.file, 'r', encoding='utf-8')
			except Exception as e:
				self.warn(u'cant open pipe file {0}: {1}'.format(self.file, traceback.format_exc(e)))
				handle.close()
				return
			
			try:
				
				for line in handle:
					try:
						line = line.rstrip()
						if line:
							self.process_line(line)
					except Exception as e:
						line = "".join(i if ord(i) < 128 else '?' for i in s)
						self.warn(u'bad line "{0}": {1}'.format(line, traceback.format_exc(e)))
				
			except Exception as e:
				self.warn(u'error reading file {0}: {1}'.format(self.file, traceback.format_exc(e)))
				
			finally:
				handle.close()
	
	
	def start(self):
		if self.thread is not None:
			return
		self.thread = threading.Thread(target=self.run)
		self.thread.daemon = True
		self.thread.start()
	
	def stop(self):
		# Signal the thread to exit
		self.running = False
		open(self.file, 'w').close()
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
		pipe.start()
		bot.debug(__file__, 'Pipe {0}: {1}'.format(pipe.name, pipe.file), DEBUG)
	
	bot.memory['pipes'] = pipes


def shutdown(bot):
	
	for pipe in bot.memory['pipes']:
		pipe.stop()
	
	bot.memory['pipes'] = None
