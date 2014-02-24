# -*- coding: utf-8 -*-
"""
staticrss.py - Willie RSS Module with static configuration
Copyright © 2014, Daniel Scharrer, <daniel@constexpr.org>
Copyright 2012, Michael Yanovich, yanovich.net (original RSS module)
Licensed under the Eiffel Forum License 2.

This is a modification of the standard Willie RSS module with the folloing changes:
- Is configured via config file, exposes no commands
- Does not require database for feed state (ignores all messages before startup)
- Supports multiple messages between polls
- Allows customizing feed titles and URLs
- Backoff instead of completely disabling broken feeds
"""

from datetime import datetime
import time
import re
import socket
import threading
import feedparser
from copy import copy
from willie.module import interval
from willie.config import ConfigurationError


socket.setdefaulttimeout(10)

INTERVAL = 5 * 60 # seconds between checking for new updates
MAX_ITEMS = 5
DEBUG = 'verbose'
MAX_LINE_LENGTH = 390


class Feed:
	
	
	def __init__(self):
		self.name = '(default)'
		self.url = None
		self.interval = 0
		self.age = 0
		self.title_pattern = re.compile(r'(.*)')
		self.title_format = None
		self.link_pattern = re.compile(r'(.*)')
		self.link_format = None
		self.exclude = set()
		self.enable = set()
		self.old_items = None
		self.old_time = 0
		self.backoff = 0
		self.etag = None
		self.modified = None
		self.lock = threading.Lock()
	
	
	def parse_config(self, section):
		
		if section.url:
			self.url = section.url
		
		if section.interval:
			self.interval = int(section.interval) * 60
		
		if section.title_pattern:
			self.title_pattern = re.compile(section.title_pattern)
		
		if section.title_format:
			self.title_format = section.title_format
		
		if section.link_pattern:
			self.link_pattern = re.compile(section.link_pattern)
		
		if section.link_format:
			self.link_format = section.link_format
		
		exclude = section.get_list('exclude')
		if exclude:
			self.exclude = set(exclude)
		
		enable = section.get_list('enable')
		if enable:
			self.enable = set(enable)
	
	
	def validate_config(self):
		
		if not self.url:
			raise ConfigurationError('Missing rss url for feed {0}'.format(self.name))
		
		if self.interval < 0:
			raise ConfigurationError(
				'Invalid rss update interval {0} for feed {1}'.format(self.interval, self.name))
	
	
	def disable(self):
		self.backoff += self.interval + (self.backoff / 10)
	
	
	def msg(self, bot, message):
		
		channels = self.enable if self.enable is not None else bot.privileges
		for channel in channels:
			
			if not channel or channel[0] != '#':
				return # Invalid channel
			
			if channel in self.exclude:
				return # Excluded channel
			
			bot.msg(channel, message)
	
	
	def new_item(self, bot, item):
		
		title = item.title if 'title' in item else '';
		if self.title_format:
			title = self.title_pattern.sub(self.title_format, title)
		
		max_length = MAX_LINE_LENGTH
		link = ''
		if 'link' in item:
			link = item.link
			if self.link_format:
				link = self.link_pattern.sub(self.link_format, link)
			if link and title:
				link = ' - ' + link
			max_length -= len(link)
		
		if title and len(title) > max_length:
			title = title[:max_length - 2] + ' …'
		
		message = title + link
		if message:
			self.msg(bot, message)
	
	
	@staticmethod
	def guid(item):
		if 'guid' in item:
			return item.guid
		guid = ''
		if 'link' in item:
			guid += item.link + '#'
		if 'published' in item:
			guid += item.published
		return guid
	
	
	def update(self, bot, elapsed_seconds):
		
		# Support per-feed update interval
		self.age += elapsed_seconds
		if self.age < self.interval + self.backoff:
			return
		self.age = self.age % self.interval
		
		# Download feed snapshot
		try:
			fp = feedparser.parse(self.url, etag=self.etag, modified=self.modified)
		except IOError as e:
			bot.debug(__file__, 'Can\'t parse feed on {0}, disabling ({1})'.format(
				self.name, str(e)), 'warning')
			return self.disable()
		
		# fp.status will only exist if pulling from an online feed
		status = getattr(fp, 'status', None)
		
		# Check for malformed XML
		if fp.bozo:
			bot.debug(__file__, 'Got malformed feed on {0}, disabling ({1})'.format(
				self.name, fp.bozo_exception.getMessage()), 'warning')
			return self.disable()
		
		# Check HTTP status
		if status == 301: # MOVED_PERMANENTLY
			bot.debug(__file__,
				'Got HTTP 301 (Moved Permanently) on {0}, updating URI to {1}'.format(
				self.name, fp.href), 'warning')
			self.url = fp.href
		if status == 410: # GONE
			bot.debug(__file__, 'Got HTTP 410 (Gone) on {0}, disabling'.format(
				self.name), 'warning')
			return self.disable()
		if status == 304: # NOT MODIFIED
			bot.debug(__file__, 'Got HTTP 304 (Not Modified) on {0}'.format(self.name), DEBUG)
			return
		
		# Check if anything changed
		new_etag = fp.etag if hasattr(fp, 'etag') else None
		if new_etag is not None and new_etag == self.etag:
			bot.debug(__file__, 'Same ETAG: {0} "{1}"'.format(self.name, new_etag), DEBUG)
			return
		new_modified = fp.modified if hasattr(fp, 'modified') else None
		if new_modified is not None and new_modified == self.modified:
			bot.debug(__file__, 'Same modification time: {0} {1}'.format(
				self.name, new_modified), DEBUG)
			return
		
		bot.debug(__file__,
			'{0}: status = {1}, version = {2}, items = {3}, etag = "{4}", time = {5}'.format(
			self.name, status, fp.version, len(fp.entries), new_etag, new_modified), DEBUG)
		self.backoff = 0
		
		# Check for new items
		if fp.entries and self.old_items is not None:
			skipped = -MAX_ITEMS
			for item in reversed(fp.entries):
				if self.guid(item) in self.old_items:
					continue
				if 'published' in item:
					new_time = time.mktime(item.published_parsed)
					if new_time <= self.old_time:
						bot.debug(__file__, 'Old ptime: {0}: {1} <= {2} "{3}"'.format(
							self.name, new_time, self.old_time, self.guid(item)), 'warning')
						continue
				if skipped < 0:
					self.new_item(bot, item)
				skipped += 1
			if skipped == 1:
				self.msg(bot, '(and one more item)')
			elif skipped > 0:
				self.msg(bot, '(and {0} more items)'.format(skipped))
		
		# Update the known items list
		if fp.entries or self.old_items is None:
			self.old_items = set()
			for item in reversed(fp.entries):
				self.old_items.add(self.guid(item))
			if fp.entries and 'published' in fp.entries[0]:
				self.old_time = max(self.old_time, time.mktime(fp.entries[0].published_parsed))
		
		# Update the last update time
		self.etag = new_etag
		self.modified = new_modified


def setup(bot):
	
	if not bot.config.has_section('rss'):
		raise ConfigurationError('Missing rss config section')
	
	defaults = Feed()
	defaults.parse_config(bot.config.rss)
	
	feeds = [ ]
	
	names = bot.config.rss.get_list('feeds')
	if not names:
		feeds.append(defaults)
	else:
		for name in names:
			
			section = 'rss_' + name
			if not bot.config.has_section(section):
				raise ConfigurationError('Missing rss config section for feed {0}'.format(name))
			
			feed = copy(defaults)
			feed.name = name
			feed.parse_config(getattr(bot.config, section))
			
			feeds.append(feed)
	
	for feed in feeds:
		feed.validate_config()
		bot.debug(__file__, 'RSS Feed: {0} {1} {2}'.format(
			feed.name, feed.url, feed.interval), DEBUG)
	
	bot.memory['staticrss'] = feeds


@interval(INTERVAL)
def update_feeds(bot):
	for feed in bot.memory['staticrss']:
		try:
			feed.lock.acquire()
			feed.update(bot, INTERVAL)
		finally:
			feed.lock.release()

