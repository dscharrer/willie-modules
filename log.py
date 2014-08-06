# -*- coding: utf8 -*-
"""
log.py - Minimal Willie logging module
Copyright © 2014, Daniel Scharrer, <daniel@constexpr.org>
Licensed under the Eiffel Forum License 2.

This module implements a simple channel log
"""
import time
import os
import re
import codecs
import threading
import traceback
from types import MethodType
from willie.module import event, rule, priority
from willie.tools import Nick
from willie.config import ConfigurationError


def configure(config):
	"""
	| [log] | example | purpose |
	| --------- | ------- | ------- |
	| exclude | #willie | A list of channels which should not be logged |
	| enable | #willie | A whitelist of the only channels you want to log |
	| path | /home/willie/logs | Base directory for log files |
	"""
	if config.option('Configure log', False):
		config.interactive_add('log', 'exclude', "A list of channels which should not be logged")
		config.interactive_add('log', 'enable', "A whitelist of the only channels you want to log")
		config.interactive_add('log', 'path', "Base directory for log files")

def add_filter(bot, method):
	def filtered_write(self, *args, **kwargs):
		filter(self, *args, **kwargs)
		return method(*args, **kwargs)
	return MethodType(filtered_write, bot, type(bot))

class Logfile:
	def __init__(self, date, handle):
		self.date = date
		self.handle = handle

class Logger:
	
	def __init__(self, exclude, enable):
		self.exclude = exclude
		self.enable = enable
		self.files = { }
		self.lock = threading.Lock()
	
	def get_logfile(self, bot, channel, timestamp):
		
		date = time.strftime('%Y-%m-%d', timestamp)
		
		if channel in self.files:
			logfile = self.files[channel]
			if logfile.date == date:
				return logfile.handle
			else:
				logfile.handle.close()
		
		basepath = bot.config.log.path + '/' + channel[1:] + '/'
		path = basepath + time.strftime('%Y', timestamp)
		if not os.path.isdir(path):
			try:
				bot.debug(__file__, u'Creating log directory {0}'.format(path), 'verbose')
				os.makedirs(path)
			except Exception as e:
				bot.debug(__file__, u'Cant create log directory {0}'.format(path), 'warning')
				self.exclude.append(channel)
				return
		
		filename = path + '/' + channel + '.' + date + '.log'
		logfile = None
		try:
			bot.debug(__file__, u'Opening log file {0}'.format(filename), 'verbose')
			logfile = codecs.open(filename, 'a', encoding='utf-8')
		except Exception as e:
			bot.debug(__file__, u'Cant open log file {0}'.format(filename), 'warning')
		
		try:
			today = basepath + "today.log"
			target = os.path.relpath(filename, basepath)
			if not os.path.islink(today) or os.readlink(today) != target:
				if os.path.islink(today):
					yesterday = basepath + "yesterday.log"
					if os.path.islink(yesterday):
						os.remove(yesterday)
					os.rename(today, yesterday)
				os.symlink(target, today)
		except Exception as e:
			bot.debug(__file__, u'Cant update symlinks for {0}: {1}'.format(filename, str(e)),
				'warning')
		
		self.files[channel] = Logfile(date, logfile)
		return logfile
	
	# Write a message to the text log file
	def log(self, bot, channel, msg, *args):
		
		# Ignore messages to users
		if channel[0] != '#':
			return
		
		# Normalize channel
		channel = channel.lower()
		
		# Ignore unknown channels
		if not channel in bot.privileges and not channel in bot.channels:
			return
		
		# Apply whitelist, if present
		if self.enable and channel not in self.enable:
			return
		# Apply blacklist, if present
		if channel in self.exclude:
			return
		
		try:
			self.lock.acquire()
			
			timestamp = time.gmtime()
			
			logfile = self.get_logfile(bot, channel, timestamp)
			if not logfile:
				return
			
			msg = unicode(msg).format(*args)
			msg = time.strftime('[%Y-%m-%d] %H:%M:%S  ', timestamp) + msg
			msg = msg + '\n'
			logfile.write(msg)
			logfile.flush()
			
		finally:
			self.lock.release()
	
	def close(self):
		for logfile in self.files:
			logfile.handle.close()
		self.files = { }

def setup(bot):
	
	os.umask(0022)
	
	if not bot.config.has_section('log'):
		raise ConfigurationError('missing log config section')
	
	exclude = set(bot.config.log.get_list('exclude'))
	enable = set(bot.config.log.get_list('enable'))
	
	if not bot.config.log.path:
		raise ConfigurationError('missing path in log config section')
	if not os.path.isdir(bot.config.log.path):
		try:
			bot.debug(__file__, u'Creating log directory {0}'.format(bot.config.log.path), 'verbose')
			os.makedirs(bot.config.log.path)
		except Exception as e:
			raise
	
	# TODO: Evil hack - because Willie doesn't support outgoing message filters, inject our own
	write = getattr(bot, 'write');
	setattr(bot, 'write', add_filter(bot, write))
	
	bot.memory['logger'] = Logger(exclude, enable)
	bot.memory['logger_restore_write'] = write

def shutdown(bot):
	try:
		setattr(bot, 'write', bot.memory['logger_restore_write'])
		bot.memory['logger'].close()
		bot.memory['logger'] = None
	except Exception as e:
		bot.bot.debug(__file__, u'Error closing log: {0}'.format(traceback.format_exc(e)), 'warning')

# Write a message to the text log file
def log(bot, channel, msg, *args):
	logger = bot.memory['logger']
	if logger is not None:
		logger.log(bot, channel, msg, *args)

@event('JOIN')
@rule(r'.*')
@priority('low')
def on_join(bot, trigger):
	"""Log a user joining the channel."""
	for channel in trigger.args[0].split(','):
		log(bot, channel, '*** {} has joined {}', trigger.nick, channel);

@event('PART')
@rule(r'.*')
@priority('low')
def on_part(bot, trigger):
	"""Log a user leaving a channel."""
	for channel in trigger.args[0].split(','):
		log(bot, channel, '*** {} has left left {}', trigger.nick, channel);

@event('QUIT')
@rule(r'.*')
@priority('low')
def on_quit(bot, trigger):
	"""Log a user quitting irc."""
	for channel in bot.privileges:
		log(bot, channel, '*** {} has quit IRC', trigger.nick);

@event('KICK')
@rule(r'.*')
@priority('low')
def on_kick(bot, trigger):
	if len(trigger.args) == 3:
		(channel, target, kickmsg) = trigger.args
	else:
		(channel, target) = trigger.args
		kickmsg = ''
	if kickmsg:
		log(bot, channel, '*** {} was kicked by {} ({})', target, trigger.nick, kickmsg)
	else:
		log(bot, channel, '*** {} was kicked by {}', target, trigger.nick)

@event('NICK')
@rule(r'.*')
@priority('low')
def on_nick_change(bot, trigger):
	"""Log a nick change."""
	old_nick = trigger.nick
	new_nick = Nick(trigger.args[0])
	for channel in bot.privileges:
		if new_nick in bot.privileges[channel]:
			log(bot, channel, '*** {} is now known as {}', old_nick, new_nick);

@event('TOPIC')
@rule(r'.*')
@priority('low')
def on_topic_change(bot, trigger):
	"""Log a topic change."""
	if len(trigger.args) == 1:
		return # Empty TOPIC gets the current topic.
	channel = trigger.args[0]
	log(bot, channel, '*** {} changes topic to "{}"', trigger.nick, trigger.args[1]);

@event('MODE')
@rule(r'.*')
@priority('low')
def on_mode_change(bot, trigger):
	# If the first character of where the mode is being set isn't a #
	# then it's a user mode, not a channel mode, so we'll ignore it.
	if trigger.args[0][0] != '#' or not trigger.args[1:]:
		return
	channel, mode_sec = trigger.args[:2]
	nicks = [Nick(n) for n in trigger.args[2:]]
	log(bot, channel, '*** {} sets mode: {} {}', trigger.nick or trigger.host, mode_sec,
	    ' '.join(nicks));

@event('NOTICE')
@rule(r'.*')
@priority('low')
def on_notice(bot, trigger):
	recipients = trigger.args[0]
	for channel in recipients.split(','):
		if channel and channel[0] == '#':
			log(bot, channel, '-{}- {}', trigger.nick, trigger)

def is_ctcp(msg):
	return msg.startswith('\x01') and msg.endswith('\x01') and len(msg) > 2

def is_action(msg):
	if is_ctcp(msg):
		payload = msg[1:-1] # chop off \x01 on both ends
		command = payload.split(None, 1)[0]
		return command == 'ACTION'
	else:
		return False

action_message_re = re.compile(r'^\x01ACTION\s+(.*)\x01$')
def action_message(msg):
	return action_message_re.match(msg).group(1)

@rule(r'.*')
@priority('high') # so it comes before messages that result from it
def on_msg(bot, trigger):
	"""Log a user sending a message to a channel."""
	if is_action(trigger):
		log(bot, trigger.sender, '* {} {}', trigger.nick, action_message(trigger));
	else:
		log(bot, trigger.sender, '<{}> {}', trigger.nick, trigger);

class FakeTrigger(unicode):
	def __new__(cls, text, nick, sender):
		s = unicode.__new__(cls, text)
		s.sender = sender
		s.nick = nick
		return s

def filter(bot, args, text=None):
	
	if not args or len(args) < 2 or args[0] != 'PRIVMSG':
		return
	
	args = [bot.safe(arg) for arg in args]
	
	if text is not None:
		msg = bot.safe(text)
	else:
		msg = u' '.join(args[2:]).lstrip()
		if msg[0] == ':':
			msg = msg[1:]
	
	on_msg(bot, FakeTrigger(msg, bot.nick, args[1]))
