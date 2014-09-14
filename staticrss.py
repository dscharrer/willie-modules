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
import os
import socket
import threading
import feedparser
import urllib2
import urlparse
import traceback
import codecs
from copy import copy
from willie.module import interval
from willie.config import ConfigurationError
from bs4 import BeautifulSoup
from collections import namedtuple


socket.setdefaulttimeout(10)

INTERVAL = 30 # seconds between checking for new updates
MAX_LINE_LENGTH = 390

class DefaultErrorHandler(urllib2.HTTPDefaultErrorHandler):
	def http_error_default(self, req, fp, code, msg, headers):
		result = urllib2.HTTPError(req.get_full_url(), code, msg, headers, fp)
		result.status = code
		return result

class Feed:
	
	
	def __init__(self):
		self.debug = 'verbose'
		self.max_items = 5
		self.name = '(default)'
		self.url = None
		self.interval = 0
		self.age = 0
		self.soup = None
		self.title_soup = None
		self.title_pattern = re.compile(r'(.*)')
		self.title_format = None
		self.link_soup = None
		self.link_pattern = re.compile(r'(.*)')
		self.link_format = None
		self.published_soup = None
		self.exclude = set()
		self.enable = set()
		self.old_items = None
		self.old_time = 0
		self.backoff = 0
		self.etag = None
		self.modified = None
		self.state = None
	
	
	def parse_config(self, section):
		
		if section.debug:
			self.debug = section.debug
		
		if section.url:
			self.url = section.url
		
		if section.interval:
			self.interval = int(section.interval) * 60
			self.age = self.interval + 1
		
		if section.soup:
			self.soup = section.soup
		
		if section.title_soup:
			self.title_soup = section.title_soup
		
		if section.title_pattern:
			self.title_pattern = re.compile(section.title_pattern)
		
		if section.title_format:
			self.title_format = section.title_format
		
		if section.link_soup:
			self.link_soup = section.link_soup
		
		if section.link_pattern:
			self.link_pattern = re.compile(section.link_pattern)
		
		if section.link_format:
			self.link_format = section.link_format
		
		if section.published_soup:
			self.published_soup = section.published_soup
		
		exclude = section.get_list('exclude')
		if exclude:
			self.exclude = set(exclude)
		
		enable = section.get_list('enable')
		if enable:
			self.enable = set(enable)
		
		if section.state:
			self.state = section.state
		
		if section.max_items:
			self.max_items = int(section.max_items)
	
	
	def state_file(self):
		return self.state + '/' + self.name
	
	
	def load(self):
		
		if not self.url:
			raise ConfigurationError(u'Missing rss url for feed {0}'.format(self.name))
		
		if self.interval < 0:
			raise ConfigurationError(
				u'Invalid rss update interval {0} for feed {1}'.format(self.interval, self.name))
		
		if self.soup and (not self.title_soup) and (not self.link_soup):
			raise ConfigurationError(
				u'soup requires title_soup and/or link_soup for feed {1}'.format(self.name))
		
		if os.path.exists(self.state_file()):
			old_items = set()
			handle = codecs.open(self.state_file(), 'r', encoding='utf-8')
			try:
				first = True
				for line in handle:
					line = line.rstrip()
					if line:
						if first:
							first = False
							self.old_time = float(line)
						else:
							old_items.add(unicode(line))
				if not first:
					self.old_items = old_items
				
			finally:
				handle.close()
	
	
	def save(self):
		if self.old_items is not None:
			handle = codecs.open(self.state_file(), 'w', encoding='utf-8')
			try:
				handle.write(unicode(self.old_time) + u'\n')
				for guid in self.old_items:
					handle.write(guid + u'\n')
			finally:
				handle.close()
	
	
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
			return unicode(item.guid.strip().replace('\n',' '))
		guid = ''
		if 'title' in item:
			guid = item.title;
		if 'link' in item:
			guid = item.link;
		if 'published' in item:
			guid += '#' + item.published
		return unicode(guid.strip().replace('\n',' '))
	
	def update_feed(self, bot):
		
		mtime = None
		if self.url.find(':') == -1:
			mtime = os.path.getmtime(self.url)
			if self.modified and self.modified >= mtime:
				Status = namedtuple('Status', 'status')
				return Status(304)
		
		fp = feedparser.parse(self.url, etag=self.etag, modified=self.modified)
		
		# Check for malformed XML
		if fp.bozo and not isinstance(fp.bozo_exception, feedparser.CharacterEncodingOverride):
			raise fp.bozo_exception
		
		# Check HTTP status
		if getattr(fp, 'status', 200) == 410: # GONE
			raise urllib2.HTTPError(self.url, 410, 'Gone.', { }, fp)
		
		if mtime:
			setattr(fp, 'modified', mtime)
		
		return fp
	
	@staticmethod
	def get_text(blob):
		try:
			return blob.get_text().strip()
		except:
			return blob.strip()
	
	def update_soup(self, bot):
		
		request = urllib2.Request(self.url)
		
		if self.etag and not self.modified:
			request.add_header('If-None-Match', self.etag)
		
		if self.modified:
			request.add_header('If-Modified-Since', self.modified)
		
		opener = urllib2.build_opener(DefaultErrorHandler()) 
		
		fp = opener.open(request)
		
		setattr(fp, 'entries', None)
		
		if hasattr(fp, 'status'):
			if fp.status == 304 or fp.status == 301:
				return
			if fp.status != 200:
				raise fp
		
		page = BeautifulSoup(fp.read())
		
		entries = [ ]
		
		for post in eval(self.soup, {}, {"page" : page}):
			
			entry = feedparser.FeedParserDict()
			
			if self.title_soup:
				entry['title'] = self.get_text(eval(self.title_soup, {}, {"post" : post}))
			
			if self.link_soup:
				url = self.get_text(eval(self.link_soup, {}, {"post" : post}))
				entry['link'] = urlparse.urljoin(self.url, url)
			
			if self.published_soup:
				entry['published'] = self.get_text(eval(self.published_soup, {}, {"post" : post}))
			
			entries.append(entry)
		
		setattr(fp, 'entries', entries)
		
		etag = fp.headers.get('ETag')
		if etag:
			setattr(fp, 'etag', etag)
		
		modified = fp.headers.get('Last-Modified')
		if modified:
			setattr(fp, 'modified', modified)
		
		return fp
	
	def update(self, bot, elapsed_seconds):
		
		# Support per-feed update interval
		self.age += elapsed_seconds
		if self.age < self.interval + self.backoff:
			return False
		self.age = self.age % self.interval
		
		# Download feed snapshot
		try:
			if self.soup:
				fp = self.update_soup(bot)
			else:
				fp = self.update_feed(bot)
		except urllib2.HTTPError as e:
			bot.debug(__file__, u'{0}: Can\'t parse feed, disabling ({1})'.format(
				self.name, str(e)), 'warning')
			self.disable()
			return True
		except IOError as e:
			bot.debug(__file__, u'{0}: Can\'t parse feed, disabling ({1})'.format(
				self.name, str(e)), 'warning')
			self.disable()
			return True
		except Exception as e:
			bot.debug(__file__, u'{0}: Can\'t parse feed, disabling: {1}'.format(
				self.name, traceback.format_exc(e)), 'warning')
			self.disable()
			return True
		
		# fp.status will only exist if pulling from an online feed
		status = getattr(fp, 'status', 200)
		
		self.backoff = 0
		
		# Check HTTP status
		if status == 301 and hasattr(fp, 'href'): # MOVED_PERMANENTLY
			bot.debug(__file__,
				u'{0}: status = 301 (Moved Permanently), updating URI to {1}'.format(
				self.name, fp.href), 'warning')
			self.url = fp.href
		if status == 304: # NOT MODIFIED
			bot.debug(__file__, u'{0}: status = 304 (Not Modified)'.format(self.name),
				self.debug)
			return True
		
		# Check if anything changed
		new_etag = fp.etag if hasattr(fp, 'etag') else None
		if new_etag is not None and new_etag == self.etag:
			bot.debug(__file__, u'{0}: Same etag: {1}'.format(self.name, new_etag), self.debug)
			return True
		new_modified = fp.modified if hasattr(fp, 'modified') else None
		if new_modified is not None and new_modified == self.modified:
			bot.debug(__file__, u'{0}: Same modification time: {1}'.format(
				self.name, new_modified), self.debug)
			return True
		
		bot.debug(__file__,
			u'{0}: status = {1}, items = {2}, etag = {3}, time = {4}'.format(
			self.name, status, len(fp.entries), new_etag, new_modified), self.debug)
		
		# Check for new items
		new_items = (self.old_items is None)
		if fp.entries and self.old_items is not None:
			skipped = -self.max_items
			for item in reversed(fp.entries):
				guid = self.guid(item)
				if guid in self.old_items:
					continue
				new_items = True
				if 'published_parsed' in item:
					new_time = time.mktime(item.published_parsed)
					if new_time <= self.old_time:
						bot.debug(__file__, u'{0}: Old ptime: {1} <= {2} "{3}"'.format(
							self.name, new_time, self.old_time, guid), 'warning')
						continue
				if skipped < 0:
					bot.debug(__file__, u'{0}: New item: "{3}"'.format(self.name, guid), self.debug)
					self.new_item(bot, item)
				skipped += 1
			if skipped == 1:
				self.msg(bot, u'(and one more item)')
			elif skipped > 0:
				self.msg(bot, u'(and {0} more items)'.format(skipped))
		
		# Update the known items list
		if new_items:
			self.old_items = set()
			for item in reversed(fp.entries):
				self.old_items.add(self.guid(item))
			if fp.entries and 'published_parsed' in fp.entries[0]:
				self.old_time = max(self.old_time, time.mktime(fp.entries[0].published_parsed))
			self.save()
		
		# Update the last update time
		self.etag = new_etag
		self.modified = new_modified
		
		return True


class Feeds:
	
	def __init__(self, feeds):
		self.feeds = feeds
		self.next = 0
		self.lock = threading.Lock()


def setup(bot):
	
	if not bot.config.has_section('rss'):
		raise ConfigurationError(u'Missing rss config section')
	
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
				raise ConfigurationError(u'Missing rss config section for feed {0}'.format(name))
			
			feed = copy(defaults)
			feed.name = name
			feed.parse_config(getattr(bot.config, section))
			
			feeds.append(feed)
	
	for feed in feeds:
		feed.load()
		bot.debug(__file__, u'{0}: {1} {2} @{3} #={4} >={5}'.format(
			u'Soup' if feed.soup else u'Feed',
			feed.name, feed.url, feed.interval,
			len(feed.old_items) if feed.old_items is not None else None, feed.old_time),
			feed.debug)
	
	bot.memory['staticrss'] = Feeds(feeds)


@interval(INTERVAL)
def update_feeds(bot):
	
	data = bot.memory['staticrss']
	
	with data.lock:
		
		for i in [(i + data.next) % len(data.feeds) for i in range(len(data.feeds))]:
			data.next = i + 1
			if data.feeds[i].update(bot, INTERVAL):
				return


def shutdown(bot):
	
	data = bot.memory['staticrss']
	
	with data.lock:
		
		for feed in data.feeds:
			try:
				feed.save()
			except Exception as e:
				bot.debug(__file__, '{0}: Can\'t save feed state: {1}'.format(
					feed.name, traceback.format_exc(e)), 'warning')
