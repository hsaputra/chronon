#!/usr/bin/env python3

import argparse
import time
import json
import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime

ONLINE_ARGS = '--online-jar={online_jar} --online-class={online_class} '
OFFLINE_ARGS = '--conf-path={conf_path} --end-date={ds} '
ONLINE_WRITE_ARGS = '--conf-path={conf_path} ' + ONLINE_ARGS
ONLINE_OFFLINE_WRITE_ARGS = OFFLINE_ARGS + ONLINE_ARGS
ONLINE_MODES = ['streaming', 'metadata-upload', 'fetch', 'local-streaming']
SPARK_MODES = ['backfill', 'upload', 'streaming', 'consistency-metrics-compute',
               'analyze', 'stats-summary', 'log-flattener', 'metadata-export', 'label-join']

# Constants for supporting multiple spark versions.
SUPPORTED_SPARK = ['2.4.0', '3.1.1', '3.2.1']
SCALA_VERSION_FOR_SPARK = {
    '2.4.0': "2.11",
    '3.1.1': "2.12",
    '3.2.1': "2.13"
}

MODE_ARGS = {
    'backfill': OFFLINE_ARGS,
    'upload': OFFLINE_ARGS,
    'stats-summary': OFFLINE_ARGS,
    'analyze': OFFLINE_ARGS,
    'streaming': ONLINE_WRITE_ARGS,
    'metadata-upload': ONLINE_WRITE_ARGS,
    'fetch': ONLINE_ARGS,
    'consistency-metrics-compute': OFFLINE_ARGS,
    'local-streaming': ONLINE_WRITE_ARGS + ' -d',
    'log-flattener': OFFLINE_ARGS,
    'metadata-export': OFFLINE_ARGS,
    'label-join': OFFLINE_ARGS,
}

ROUTES = {
    'group_bys': {
        'upload': 'group-by-upload',
        'backfill': 'group-by-backfill',
        'streaming': 'group-by-streaming',
        'local-streaming': 'group-by-streaming',
        'fetch': 'fetch',
        'analyze': 'analyze',
        'metadata-export': 'metadata-export',
    },
    'joins': {
        'backfill': 'join',
        'metadata-upload': 'metadata-upload',
        'fetch': 'fetch',
        'consistency-metrics-compute': 'consistency-metrics-compute',
        'stats-summary': 'stats-summary',
        'analyze': 'analyze',
        'log-flattener': 'log-flattener',
        'metadata-export': 'metadata-export',
        'label-join': 'label-join',
    },
    'staging_queries': {
        'backfill': 'staging-query-backfill',
        'metadata-export': 'metadata-export',
    },
}

APP_NAME_TEMPLATE = "chronon_{conf_type}_{mode}_{context}_{name}"


def retry_decorator(retries=3, backoff=20):
    def wrapper(func):
        def wrapped(*args, **kwargs):
            attempt = 0
            while attempt <= retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    logging.exception(e)
                    sleep_time = attempt * backoff
                    logging.info(
                        "[{}] Retry: {} out of {}/ Sleeping for {}"
                        .format(func.__name__, attempt, retries, sleep_time))
                    time.sleep(sleep_time)
            return func(*args, **kwargs)
        return wrapped
    return wrapper


def check_call(cmd):
    print("Running command: " + cmd)
    return subprocess.check_call(cmd.split(), bufsize=0)


def check_output(cmd):
    print("Running command: " + cmd)
    return subprocess.check_output(cmd.split(), bufsize=0).strip()


def download_only_once(url, path):
    should_download = True
    path = path.strip()
    if os.path.exists(path):
        content_output = check_output("curl -sI " + url).decode('utf-8')
        content_length = re.search("(content-length:\\s)(\\d+)", content_output.lower())
        remote_size = int(content_length.group().split()[-1])
        local_size = int(check_output("wc -c " + path).split()[0])
        print("""Files sizes of {url} vs. {path}
    Remote size: {remote_size}
    Local size : {local_size}""".format(**locals()))
        if local_size == remote_size:
            print("Sizes match. Assuming its already downloaded.")
            should_download = False
        if should_download:
            print("Different file from remote at local: " + path + ". Re-downloading..")
            check_call('curl {} -o {} --connect-timeout 10'.format(url, path))
    else:
        print("No file at: " + path + ". Downloading..")
        check_call('curl {} -o {} --connect-timeout 10'.format(url, path))


@retry_decorator(retries=3, backoff=50)
def download_jar(version, jar_type='uber', release_tag=None, spark_version="2.4.0"):
    assert spark_version in SUPPORTED_SPARK, (
        f"Received unsupported spark version {spark_version}. Supported spark versions are {SUPPORTED_SPARK}")
    scala_version = SCALA_VERSION_FOR_SPARK[spark_version]
    maven_url_prefix = os.environ.get('CHRONON_MAVEN_MIRROR_PREFIX', None)
    default_url_prefix = "https://s01.oss.sonatype.org/service/local/repositories/public/content"
    url_prefix = maven_url_prefix if maven_url_prefix else default_url_prefix
    base_url = "{}/ai/chronon/spark_{}_{}".format(
        url_prefix,
        jar_type,
        scala_version)
    print("Downloading jar from url: " + base_url)
    jar_path = os.environ.get('CHRONON_DRIVER_JAR', None)
    if jar_path is None:
        if version == 'latest':
            version = None
        if version is None:
            metadata_content = check_output("curl -s {}/maven-metadata.xml".format(base_url))
            meta_tree = ET.fromstring(metadata_content)
            versions = [
                node.text
                for node in meta_tree.findall("./versioning/versions/")
                if re.search(
                    r"^\d+\.\d+\.\d+{}$".format('\_{}\d*'.format(release_tag) if release_tag else ''),
                    node.text
                )
            ]
            version = versions[-1]
        jar_url = "{base_url}/{version}/spark_{jar_type}_{scala_version}-{version}-assembly.jar".format(
            base_url=base_url,
            version=version,
            scala_version=scala_version,
            jar_type=jar_type
        )
        jar_path = os.path.join('/tmp', jar_url.split('/')[-1])
        download_only_once(jar_url, jar_path)
    return jar_path


def set_runtime_env(args):
    """
    Setting the runtime environment variables.
    These are extracted from the common env, the team env and the common env.
    In order to use the environment variables defined in the configs as overrides for the args in the cli this method
    needs to be run before the runner and jar downloads.

    The order of priority is:
        - Environment variables existing already.
        - Environment variables derived from args (like app_name)
        - conf.metaData.modeToEnvMap for the mode (set on config)
        - team environment per context and mode set on teams.json
        - default team environment per context and mode set on teams.json
        - Common Environment set in teams.json
    """
    environment = {
        "common_env": {},
        "conf_env": {},
        "default_env": {},
        "team_env": {},
        "cli_args": {},
    }
    if args.repo:
        teams_file = os.path.join(args.repo, 'teams.json')
        if os.path.exists(teams_file):
            with open(teams_file, 'r') as infile:
                teams_json = json.load(infile)
            environment['common_env'] = teams_json.get('default', {}).get("common_env", {})
            if args.conf and args.mode:
                try:
                    context, conf_type, team, _ = args.conf.split('/')[-4:]
                except Exception as e:
                    logging.error(
                        "Invalid conf path: {}, please ensure to supply the relative path to zipline/ folder"
                        .format(args.conf))
                    raise e
                logging.info(f"Context: {context} -- conf_type: {conf_type} -- team: {team}")
                conf_path = os.path.join(args.repo, args.conf)
                with open(conf_path, 'r') as conf_file:
                    conf_json = json.load(conf_file)
                environment['conf_env'] = conf_json.get('metaData').get('modeToEnvMap', {}).get(args.mode, {})
                environment['team_env'] = teams_json[team].get(context, {}).get(args.mode, {})
                environment['default_env'] = teams_json.get('default', {}).get(context, {}).get(args.mode, {})
                environment['cli_args']['APP_NAME'] = APP_NAME_TEMPLATE.format(
                    mode=args.mode,
                    conf_type=conf_type,
                    context=context,
                    name=conf_json['metaData']['name'])
                environment['cli_args']["CHRONON_CONF_PATH"] = conf_path
    if args.app_name:
        environment['cli_args']['APP_NAME'] = args.app_name
    else:
        if args.mode == 'metadata-export':
            environment['cli_args']['APP_NAME'] = 'chronon_metadata_export'
    # Adding these to make sure they are printed if provided by the environment.
    environment['cli_args']['CHRONON_DRIVER_JAR'] = args.chronon_jar
    environment['cli_args']['CHRONON_ONLINE_JAR'] = args.online_jar
    environment['cli_args']['CHRONON_ONLINE_CLASS'] = args.online_class
    order = ['conf_env', 'team_env', 'default_env', 'common_env', 'cli_args']
    print("Setting env variables:")
    for key in os.environ:
        if any([key in environment[set_key] for set_key in order]):
            print(f"From <environment> found {key}={os.environ[key]}")
    for set_key in order:
        for key, value in environment[set_key].items():
            if key not in os.environ and value is not None:
                print(f"From <{set_key}> setting {key}={value}")
                os.environ[key] = value


class Runner:
    def __init__(self, args, jar_path):
        self.repo = args.repo
        self.conf = args.conf
        self.sub_help = args.sub_help
        self.mode = args.mode
        self.online_jar = args.online_jar
        valid_jar = args.online_jar and os.path.exists(args.online_jar)
        # fetch online jar if necessary
        if (self.mode in ONLINE_MODES) and (not args.sub_help) and not valid_jar:
            print("Downloading online_jar")
            self.online_jar = check_output("{}".format(args.online_jar_fetch)).decode("utf-8")
            os.environ['CHRONON_ONLINE_JAR'] = self.online_jar
            print("Downloaded jar to {}".format(self.online_jar))

        if self.conf:
            try:
                self.context, self.conf_type, self.team, _ = self.conf.split('/')[-4:]
            except Exception as e:
                logging.error(
                    "Invalid conf path: {}, please ensure to supply the relative path to zipline/ folder"
                    .format(self.conf))
                raise e
            possible_modes = ROUTES[self.conf_type].keys()
            assert args.mode in possible_modes, "Invalid mode:{} for conf:{} of type:{}, please choose from {}".format(
                args.mode, self.conf, self.conf_type, possible_modes)
        else:
            self.conf_type = args.conf_type
        self.ds = args.ds
        self.jar_path = jar_path
        self.args = args.args if args.args else ''
        self.online_class = args.online_class
        self.app_name = args.app_name
        if self.mode == 'streaming':
            self.spark_submit = args.spark_streaming_submit_path
        else:
            self.spark_submit = args.spark_submit_path
        self.list_apps_cmd = args.list_apps

    def run(self):
        base_args = MODE_ARGS[self.mode].format(
            conf_path=self.conf, ds=self.ds, online_jar=self.online_jar, online_class=self.online_class)
        final_args = base_args + ' ' + str(self.args)
        subcommand = ROUTES[self.conf_type][self.mode]
        if self.sub_help or (self.mode not in SPARK_MODES):
            command = 'java -cp {jar} ai.chronon.spark.Driver {subcommand} {args}'.format(
                jar=self.jar_path,
                args='--help' if self.sub_help else final_args,
                subcommand=subcommand
            )
        else:
            if self.mode in ('streaming'):
                print("Checking to see if a streaming job by the name {} already exists".format(self.app_name))
                running_apps = check_output("{}".format(self.list_apps_cmd)).decode("utf-8").split('\n')
                running_app_map = {}
                for app in running_apps:
                    try:
                        app_json = json.loads(app.strip())
                        app_name = app_json['app_name'].strip()
                        if app_name not in running_app_map:
                            running_app_map[app_name] = []
                        running_app_map[app_name].append(app_json)
                    except Exception as ex:
                        print("failed to process line into app: " + app)
                        print(ex)

                filtered_apps = running_app_map.get(self.app_name, [])
                if len(filtered_apps) > 0:
                    print("Found running apps by the name {} in \n{}\n".format(
                        self.app_name, '\n'.join([str(app) for app in filtered_apps])))
                    assert len(filtered_apps) == 1, "More than one found, please kill them all"
                    print("All good. No need to start a new app.")
                    return
            command = 'bash {script} --class ai.chronon.spark.Driver {jar} {subcommand} {args}'.format(
                script=self.spark_submit,
                jar=self.jar_path,
                subcommand=subcommand,
                args=final_args
            )
        check_call(command)


if __name__ == "__main__":
    today = datetime.today().strftime('%Y-%m-%d')
    parser = argparse.ArgumentParser(description='Submit various kinds of chronon jobs')
    chronon_repo_path = os.environ.get('CHRONON_REPO_PATH', '.')
    parser.add_argument('--conf', required=False, help='Conf param - required for every mode except fetch')
    parser.add_argument('--mode', choices=MODE_ARGS.keys(), default='backfill')
    parser.add_argument('--ds', default=today)
    parser.add_argument('--app-name', help='app name. Default to {}'.format(APP_NAME_TEMPLATE),
                        default=os.environ.get('APP_NAME', None))
    parser.add_argument('--repo', help='Path to chronon repo', default=chronon_repo_path)
    parser.add_argument('--online-jar',
                        help='Jar containing Online KvStore & Deserializer Impl. ' +
                             'Used for streaming and metadata-upload mode.',
                        default=os.environ.get('CHRONON_ONLINE_JAR', None))
    parser.add_argument('--online-class',
                        help='Class name of Online Impl. Used for streaming and metadata-upload mode.',
                        default=os.environ.get('CHRONON_ONLINE_CLASS', None))
    parser.add_argument('--version', help='Chronon version to use.', default=os.environ.get('VERSION', None)),
    parser.add_argument('--spark-version', help='Spark version to use for downloading jar.',
                        default=os.environ.get('SPARK_VERSION', '2.4.0')),
    parser.add_argument('--spark-submit-path',
                        help='Path to spark-submit',
                        default=os.path.join(chronon_repo_path, 'scripts/spark_submit.sh'))
    parser.add_argument('--spark-streaming-submit-path',
                        help='Path to spark-submit for streaming',
                        default=os.path.join(chronon_repo_path, 'scripts/spark_streaming.sh'))
    parser.add_argument('--online-jar-fetch',
                        help='Path to script that can pull online jar. ' +
                             "This will run only when a file doesn't exist at location specified by online_jar",
                        default=os.path.join(chronon_repo_path, 'scripts/fetch_online_jar.py'))
    parser.add_argument('--sub-help', action='store_true', help='print help command of the underlying jar and exit')
    parser.add_argument('--conf-type', default='group_bys',
                        help='related to sub-help - no need to set unless you are not working with a conf')
    parser.add_argument('--online-args', default=os.environ.get('CHRONON_ONLINE_ARGS', ''),
                        help='Basic arguments that need to be supplied to all online modes')
    parser.add_argument('--chronon-jar', default=os.environ.get('CHRONON_DRIVER_JAR'), help='Path to chronon OS jar')
    parser.add_argument('--release-tag', default=None, help='Use the latest jar for a particular tag.')
    parser.add_argument('--list-apps', default="python3 " + os.path.join(chronon_repo_path, "scripts/yarn_list.py"),
                        help='command/script to list running jobs on the scheduler')
    pre_parse_args, _ = parser.parse_known_args()
    # We do a pre-parse to extract conf, mode, etc and set environment variables that may override default values.
    set_runtime_env(pre_parse_args)
    args, unknown_args = parser.parse_known_args()
    jar_type = 'embedded' if args.mode == 'local-streaming' else 'uber'
    extra_args = (' ' + args.online_args) if args.mode in ONLINE_MODES else ''
    args.args = ' '.join(unknown_args) + extra_args
    jar_path = args.chronon_jar if args.chronon_jar else download_jar(
        args.version, jar_type=jar_type,
        release_tag=args.release_tag,
        spark_version=args.spark_version)
    Runner(args, jar_path).run()
