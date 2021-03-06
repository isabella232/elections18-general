#!/usr/bin/env python
# _*_ coding:utf-8 _*_

import logging
import os

from fabric.api import local, require, settings, task
from fabric.state import env
from termcolor import colored

import app_config

# Other fabfiles
from . import daemons
from . import data
from . import issues
from . import render
from . import text
from . import utils

if app_config.DEPLOY_TO_SERVERS:
    from . import servers

logging.basicConfig(format=app_config.LOG_FORMAT)
logger = logging.getLogger(__name__)
logger.setLevel(app_config.LOG_LEVEL)

"""
Base configuration
"""
env.user = app_config.SERVER_USER
env.forward_agent = True

env.hosts = []

"""
Environments

Changing environment requires a full-stack test.
An environment points to both a server and an S3
bucket.
"""
@task
def production():
    """
    Run as though on production.
    """
    env.settings = 'production'
    app_config.configure_targets(env.settings)
    env.hosts = app_config.SERVERS


@task
def staging():
    """
    Run as though on staging.
    """
    env.settings = 'staging'
    app_config.configure_targets(env.settings)
    env.hosts = app_config.SERVERS


@task
def test():
    """
    Run against test DB
    """
    app_config.configure_targets('test')


"""
Branches

Changing branches requires deploying that branch to a host.
"""
@task
def stable():
    """
    Work on stable branch.
    """
    env.branch = 'stable'


@task
def master():
    """
    Work on development branch.
    """
    env.branch = 'master'


@task
def branch(branch_name):
    """
    Work on any specified branch.
    """
    env.branch = branch_name


"""
Running the app
"""
@task
def app(port='8000'):
    """
    Serve app.py.
    """
    if env.get('settings'):
        local("DEPLOYMENT_TARGET=%s bash -c 'gunicorn -b 0.0.0.0:%s --timeout 3600 --reload --log-file=logs/app.log app:wsgi_app'" % (env.settings, port))
    else:
        local('gunicorn -b 0.0.0.0:%s --timeout 3600 --reload --log-file=- app:wsgi_app' % port)


@task
def tests():
    """
    Run Python unit tests.
    """
    local('nosetests')


"""
Deployment

Changes to deployment requires a full-stack test. Deployment
has two primary functions: Pushing flat files to S3 and deploying
code to a remote server if required.
"""
@task
def publish_results():
    render.render_all()
    if env.get('settings'):
        sync_s3()
    elif os.path.isdir(app_config.GRAPHICS_DATA_OUTPUT_FOLDER):
        data.copy_data_for_graphics()
    else:
        logger.warning('No destination to publish rendered data')


@task
def sync_s3():
    local('aws s3 sync {0} s3://{1}/{2}/data/ --acl public-read --cache-control max-age=5'.format(
        app_config.DATA_OUTPUT_FOLDER,
        app_config.S3_BUCKET,
        app_config.PROJECT_SLUG
    ))


"""
Destruction

Changes to destruction require setup/deploy to a test host in order to test.
Destruction should remove all files related to the project from both a remote
host and S3.
"""
@task
def shiva_the_destroyer():
    """
    Deletes the app from s3
    """
    require('settings', provided_by=[production, staging])

    utils.confirm(
        colored("You are about to destroy everything deployed to %s for this project.\nDo you know what you're doing?')" % app_config.DEPLOYMENT_TARGET, "red")
    )

    with settings(warn_only=True):
        if app_config.DEPLOY_TO_SERVERS:
            servers.delete_project()

            servers.uninstall_crontab()
            servers.nuke_confs()
