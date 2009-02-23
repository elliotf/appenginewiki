#!/usr/bin/env python
#
# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A simple Google App Engine wiki application.

The main distinguishing feature is that editing is in a WYSIWYG editor
rather than a text editor with special syntax.  This application uses
google.appengine.api.datastore to access the datastore.  This is a
lower-level API on which google.appengine.ext.db depends.
"""

#__author__ = 'Bret Taylor'
__author__ = 'Elliot Foster'


import cgi
import datetime
import os
import re
import sys
import urllib
import urlparse
import logging

import wikimarkup

from google.appengine.api import datastore
from google.appengine.api import datastore_types
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

# for Data::Dumper-like stuff
#import pprint
#pp = pprint.PrettyPrinter(indent=4)

#lib_path = os.path.join(os.path.dirname(__file__), 'lib')
#sys.path.append(lib_path)

_DEBUG = True

class BaseRequestHandler(webapp.RequestHandler):
    def generate(self, template_name, template_values={}):
        values = {
            'request': self.request,
            'user': users.get_current_user(),
            'login_url': users.create_login_url(self.request.uri),
            'logout_url': users.create_logout_url(self.request.uri),
            'application_name': 'Piki',
        }
        values.update(template_values)
        directory = os.path.dirname(__file__)
        path = os.path.join(directory, os.path.join('templates', template_name))
        self.response.out.write(template.render(path, values, debug=_DEBUG))

    def head(self, *args):
        pass

    def get(self, *args):
        pass

    def post(self, *args):
        pass

class MainPageHandler(BaseRequestHandler):
    def get(self):
        user = users.get_current_user();

        if not user:
            self.redirect(users.create_login_url(self.request.uri))
            return

        query = datastore.Query('Page')
        query['owner'] = user
        query.Order(('modified', datastore.Query.DESCENDING))

        page_list = []
        for entity in query.Get(100):
            page_list.append(Page(entity['name'], entity))

        self.generate('index.html', {
            'pages': page_list,
        })


class PageRequestHandler(BaseRequestHandler):
    def get(self, page_name):
        # if we don't have a user, we won't know which namespace to use (for now)
        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(self.request.uri))
        page_name = urllib.unquote(page_name)
        page = Page.load(page_name, user)
        modes = ['view', 'edit']
        mode = self.request.get('mode')
        if not page.entity:
            logging.debug('page "' + page_name + '" not found, creating new instance.')
            mode = 'edit'
        if not mode in modes:
            logging.debug('defaulting mode to view')
            mode = 'view'
        self.generate(mode + '.html', {
            'page': page,
        })

    def post(self, page_name):
        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(self.request.uri))
            return

        page_name = urllib.unquote(page_name)
        page = Page.load(page_name, user)
        page.content = self.request.get('content')
        page.save()
        self.redirect(page.view_url())


class Page(object):
    """ A wiki page, has attributes:
            name
            content
            owner
            is_public -- implement later
    """
    def __init__(self, name, entity=None):
        self.name = name
        self.entity = entity
        if entity:
            self.content = entity['content']
            self.owner = entity['owner']
            self.modified = entity['modified']
        else:
            self.content = '= ' + self.name + " =\n\nStarting writing about " + self.name + ' here.'

    def entity(self):
        return self.entity

    def edit_url(self):
        return '/%s?mode=edit' % (urllib.quote(self.name))

    def view_url(self):
        name = self.name
        name = urllib.quote(name)
        return '/' + name

    def save(self):
        if self.entity:
            entity = self.entity
            logging.debug('saving existing page ' + self.name)
        else:
            logging.debug('saving new page ' + self.name)
            entity = datastore.Entity('Page')
            entity['owner'] = users.get_current_user()
            entity['name'] = self.name
        entity['content'] = datastore_types.Text(self.content)
        entity['modified'] = datetime.datetime.now()
        datastore.Put(entity)

    def wikified_content(self):
        # FIXME: check memcache?
        content = self.content
        # replacements here
        transforms = [
            AutoLink(),
            WikiWords(),
            HideReferers(),
        ]
        #content = self.content
        content = wikimarkup.parse(content)
        for transform in transforms:
            content = transform.run(content, self)
        return content


    @staticmethod
    def load(name, owner):
        if not owner:
            owner = users.get_current_user()
        query = datastore.Query('Page')
        query['name'] = name
        query['owner'] = owner
        entities = query.Get(1)
        if len(entities) < 1:
            return Page(name)
        else:
            return Page(name, entities[0])

    @staticmethod
    def exists(name, owner):
        logging.debug('looking up ' + name)
        if not owner:
            logging.debug('Were not given a user when looking up ' + name)
            owner = users.get_current_user()
        return Page.load(name, owner).entity

class Transform(object):
  """Abstraction for a regular expression transform.

  Transform subclasses have two properties:
     regexp: the regular expression defining what will be replaced
     replace(MatchObject): returns a string replacement for a regexp match

  We iterate over all matches for that regular expression, calling replace()
  on the match to determine what text should replace the matched text.

  The Transform class is more expressive than regular expression replacement
  because the replace() method can execute arbitrary code to, e.g., look
  up a WikiWord to see if the page exists before determining if the WikiWord
  should be a link.
  """
  def run(self, content, page):
    """Runs this transform over the given content.

    Args:
      content: The string data to apply a transformation to.

    Returns:
      A new string that is the result of this transform.
    """
    self.page = page
    parts = []
    offset = 0
    for match in self.regexp.finditer(content):
      parts.append(content[offset:match.start(0)])
      parts.append(self.replace(match))
      offset = match.end(0)
    parts.append(content[offset:])
    return ''.join(parts)


class WikiWords(Transform):
    """Translates WikiWords to links.

    We look up all words, and we only link those words that currently exist.
    """
    def __init__(self):
        self.regexp = re.compile(r'(\w+[/\-_])?[A-Z][a-z]+([A-Z][a-z]+)+')

    def replace(self, match):
        wikiword = match.group(0)
        logging.debug('About to look up wikiword "' + wikiword + '"')
        if wikiword == self.page.name:
            return wikiword
        if Page.exists(wikiword, self.page.owner):
            return '<a class="wikiword" href="/%s">%s</a>' % (wikiword, wikiword)
        else:
            return '<a class="wikiword missing" href="/%s">%s?</a>' % (wikiword, wikiword)


class AutoLink(Transform):
  """A transform that auto-links URLs."""
  def __init__(self):
    self.regexp = re.compile(r'([^"])\b((http|https)://[^ \t\n\r<>\(\)&"]+' \
                             r'[^ \t\n\r<>\(\)&"\.])')

  def replace(self, match):
    url = match.group(2)
    return match.group(1) + '<a class="autourl" href="%s">%s</a>' % (url, url)


class HideReferers(Transform):
  """A transform that hides referers for external hyperlinks."""

  def __init__(self):
    self.regexp = re.compile(r'href="(http[^"]+)"')

  def replace(self, match):
    url = match.group(1)
    scheme, host, path, parameters, query, fragment = urlparse.urlparse(url)
    url = 'http://www.google.com/url?sa=D&amp;q=' + urllib.quote(url)
    return 'href="%s"' % (url,)


def main():
    logging.getLogger().setLevel(logging.DEBUG)
    application = webapp.WSGIApplication(
            #[('/', MainPageHandler)],
            [
                ('/', MainPageHandler),
                ('/(.*)', PageRequestHandler),
            ],
            debug=_DEBUG,
    )
    run_wsgi_app(application)

if __name__ == '__main__':
    main()



"""
# Models
class Owner(db.Model):
    user = db.UserProperty(required=True)
    namespace = db.TextProperty()

class Page(db.Model):
    owner = db.UserProperty(required=True)
    name = db.StringProperty(required=True)
    content = db.StringProperty()
    is_public = db.BooleanProperty(default=False)

    def load(name):
        query = Page.gql("WHERE owner = :owner AND name = :name", owner=users.get_current_user(), name=name)
        return query.fetch




"""
