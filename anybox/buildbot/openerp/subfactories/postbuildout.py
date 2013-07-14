import os
from buildbot import locks
from buildbot.steps.shell import ShellCommand
from buildbot.steps.shell import SetProperty
from buildbot.steps.transfer import FileDownload
from buildbot.steps.transfer import DirectoryUpload
from buildbot.steps.master import MasterShellCommand
from buildbot.process.properties import WithProperties
from buildbot.process.properties import Property
from ..utils import comma_list_sanitize
from ..utils import bool_opt
from ..utils import BUILD_UTILS_PATH


def install_modules_test_openerp(configurator, options, buildout_slave_path,
                                 environ=()):
    """Return steps to run bin/test_openerp -i MODULES."""

    environ = dict(environ)

    steps = []

    steps.append(ShellCommand(command=['rm', '-f', 'test.log'],
                              name="Log cleanup",
                              descriptionDone=['Cleaned', 'logs'],
                              ))

    steps.append(ShellCommand(command=[
        'bin/test_openerp', '-i',
        comma_list_sanitize(options.get('openerp-addons', 'all')),
        # openerp --logfile does not work with relative paths !
        WithProperties('--logfile=%(workdir)s/build/test.log')],
        name='testing',
        description='testing',
        descriptionDone='tests',
        logfiles=dict(test='test.log'),
        haltOnFailure=True,
        env=environ,
    ))

    steps.append(ShellCommand(
        command=["python", "analyze_oerp_tests.py", "test.log"],
        name='analyze',
        description="analyze",
    ))

    return steps


def install_modules_nose(configurator, options, buildout_slave_path,
                         environ=()):
    """Install addons, run nose tests, upload result.

    Warning: this works only for addons that use the trick in main
    __init__ that avoids executing the models definition twice.

    Options:

      - openerp-addons: comma-separated list of addons to test
      - nose.tests: goes directly to command line; list directories to find
        tests here.
      - nose.coverage: boolean, if true, will run coverage for the listed
        addons
      - nose.cover-options: additional options for nosetests invocation
      - nose.upload-path: path on master to upload files produced by nose
      - nose.upload-url: URL to present files produced by nose in waterfall

    In upload-path and upload-url, one may use properties as in the
    steps definitions, with $ instead of %, to avoid ConfigParser interpret
    them.
    """

    environ = dict(environ)

    steps = []

    steps.append(ShellCommand(command=['rm', '-f', 'install.log'],
                              name="Log cleanup",
                              descriptionDone=['Cleaned', 'logs'],
                              ))
    addons = comma_list_sanitize(options.get('openerp-addons', ''))

    steps.append(ShellCommand(command=[
        'bin/start_openerp', '--stop-after-init', '-i',
        addons if addons else 'all',
        # openerp --logfile does not work with relative paths !
        WithProperties('--logfile=%(workdir)s/build/install.log')],
        name='install',
        description='install modules',
        descriptionDone='installed modules',
        logfiles=dict(log='install.log'),
        haltOnFailure=True,
        env=environ,
    ))

    steps.append(ShellCommand(
        command=["python", "analyze_oerp_tests.py", "install.log"],
        name='check',
        description="check install log",
        descriptionDone="checked install log",
    ))

    addons = addons.split(',')
    nose_output_dir = 'nose_output'
    nose_cmd = ["bin/nosetests", "-v"]
    nose_cmd.extend(options.get('nose.tests', '').split())
    upload = False

    if bool_opt(options, 'nose.coverage'):
        upload = True
        nose_cmd.append('--with-coverage')
        nose_cmd.append('--cover-html')
        nose_cmd.append('--cover-html-dir=%s' % os.path.join(
            nose_output_dir, 'coverage'))
        nose_cmd.extend(options.get(
            'nose.cover-options',
            '--cover-erase --cover-branches').split())

        for addon in addons:
            nose_cmd.extend(('--cover-package', addon))

    if bool_opt(options, 'nose.profile'):
        upload = True
        nose_cmd.extend(('--with-profile',
                         '--profile-stats-file',
                         os.path.join(nose_output_dir, 'profile.stats')))

        # sadly, restrict if always interpreted by nose as a string
        # it can't be used to limit the number of displayed lines
        # putting a default value here would make no sense.
        restrict = options.get('nose.profile-restrict')
        if restrict:
            nose_cmd.extend(('--profile-restrict', restrict))

    if upload:
        steps.append(ShellCommand(command=['mkdir', '-p', nose_output_dir],
                                  name='mkdir',
                                  description='prepare nose output',
                                  haltOnFailure=True,
                                  env=environ))

    steps.append(ShellCommand(
        command=nose_cmd,
        name='tests',
        description="nose tests",
        haltOnFailure=True,
        env=environ,
        timeout=3600*4,
    ))

    if upload:
        upload_path = options.get('nose.upload-path', '').replace('$', '%')
        upload_url = options.get('nose.upload-url', '').replace('$', '%')
        steps.append(DirectoryUpload(slavesrc=nose_output_dir,
                                     haltOnFailure=True,
                                     compress='gz',
                                     masterdest=WithProperties(upload_path),
                                     url=WithProperties(upload_url)))

        # Fixing perms on uploaded files. Yes we could have unmask = 022 in
        # all slaves, see note at the end of
        # http://buildbot.net/buildbot/docs/0.8.7/full.html#
        #     buildbot.steps.source.buildbot.steps.transfer.DirectoryUpload
        # but it's less work to fix the perms from here than to check all of
        # them
        steps.append(MasterShellCommand(
            description=["nose", "output", "read", "permissions"],
            command=['chmod', '-R', 'a+r',
                     WithProperties(upload_path)]))
        steps.append(MasterShellCommand(
            description=["nose", "output", "dirx", "permissions"],
            command=['find', WithProperties(upload_path),
                     '-type', 'd', '-exec',
                     'chmod', '755', '{}', ';']))
    return steps


port_lock = locks.SlaveLock("port-reserve")


def functional(configurator, options, buildout_slave_path,
               environ=()):
    """Reserve a port, start openerp, launch testing commands, stop openerp.

    Options:
    - functional.commands: whitespace separated list of scripts to launch.
      Each of them must accept two arguments: port and db_name
    - functional.parts: buildout parts to install to get the commands to
      work
    - functional.wait: time (in seconds) to wait for the server to be ready
      for functional testing after starting up (defaults to 30s)
    """

    steps = []

    buildout_parts = options.get('functional.parts', '').split()
    if buildout_parts:
        steps.append(ShellCommand(
            command=['bin/buildout',
                     '-c', buildout_slave_path,
                     'buildout:eggs-directory=../../buildout-caches/eggs',
                     'install'] + buildout_parts,
            name="functional tools",
            description=['install', 'functional', 'buildout', 'parts'],
            descriptionDone=['installed', 'functional',
                             'buildout', 'parts'],
            haltOnFailure=True,
            env=environ,
        ))

    steps.append(FileDownload(
        mastersrc=os.path.join(BUILD_UTILS_PATH, 'port_reserve.py'),
        slavedest='port_reserve.py'))

    steps.append(SetProperty(
        property='openerp_port',
        description=['Port', 'reservation'],
        locks=[port_lock.access('exclusive')],
        command=['python', 'port_reserve.py', '--port-min=9069',
                 '--port-max=11069', '--step=5']))

    steps.append(ShellCommand(
        command=['rm', '-f', WithProperties('%(workdir)s/openerp.pid')],
        name='cleanup',
        description='clean pid file',
        descriptionDone='cleaned pid file',
        haltOnFailure=True,
        env=environ,
    ))

    steps.append(ShellCommand(
        command=['/sbin/start-stop-daemon',
                 '--pidfile', WithProperties('%(workdir)s/openerp.pid'),
                 '--exec',
                 WithProperties('%(workdir)s/build/bin/start_openerp'),
                 '--background',
                 '--make-pidfile', '-v', '--start',
                 '--', '--xmlrpc-port', Property('openerp_port'),
                 WithProperties('--logfile=%(workdir)s/build/install.log')],
        name='start',
        description='starting openerp',
        descriptionDone='openerp started',
        haltOnFailure=True,
        env=environ,
    ))

    steps.append(ShellCommand(
        description=['Wait'],
        command=['sleep', options.get('functional.wait', '30')]))

    steps.extend(ShellCommand(
        command=[cmd, Property('openerp_port'), Property('testing_db')],
        name=cmd.rsplit('/')[-1],
        description="running %s" % cmd,
        descriptionDone="ran %s" % cmd,
        flunkOnFailure=True,
        haltOnFailure=False,
        env=environ)
        for cmd in options.get('functional.commands').split())

    steps.append(ShellCommand(
        command=['/sbin/start-stop-daemon',
                 '--pidfile', WithProperties('%(workdir)s/openerp.pid'),
                 '--stop', '--oknodo', '--retry', '5'],
        name='start',
        description='stoping openerp',
        descriptionDone='openerp stopped',
        haltOnFailure=True,
        env=environ,
    ))

    return steps
