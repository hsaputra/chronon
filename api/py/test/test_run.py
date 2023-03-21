"""
Basic tests for namespace and breaking changes in run.py
"""
from ai.chronon.repo import run
import argparse
import pytest
import time
import os


def reset_env(default_env):
    set_keys = os.environ.keys()
    for key in set_keys:
        os.environ.pop(key)
    for k, v in default_env:
        os.environ[k] = v


def test_download_jar(monkeypatch, sleepless):
    def mock_cmd(url, path):
        return url
    monkeypatch.setattr(time, 'sleep', sleepless)
    monkeypatch.setattr(run, 'download_only_once', mock_cmd)
    jar_path = run.download_jar("version", jar_type="uber", release_tag=None, spark_version='2.4.0')
    assert jar_path == "/tmp/spark_uber_2.11-version-assembly.jar"
    jar_path = run.download_jar("version", jar_type="uber", release_tag=None, spark_version='3.1.1')
    assert jar_path == "/tmp/spark_uber_2.12-version-assembly.jar"
    with pytest.raises(Exception):
        run.download_jar("version", jar_type="uber", release_tag=None, spark_version='2.1.0')


def test_environment(teams_json, repo):
    parser = argparse.ArgumentParser()
    args = ['repo', 'conf', 'mode', 'app-name', 'chronon-jar', 'online-jar', 'online-class']
    for arg in args:
        parser.add_argument(f"--{arg}")
    default_environment = os.environ
    # If nothing is passed.
    run.set_runtime_env(parser.parse_args(args=[]))

    # If repo is passed common_env is loaded.
    reset_env(default_environment)
    run.set_runtime_env(parser.parse_args(args=['--repo', repo]))
    assert os.environ['VERSION'] == 'latest'

    # For chronon_metadata_export is passed. APP_NAME should be set.
    reset_env(default_environment)
    run.set_runtime_env(parser.parse_args(args=['--mode', 'metadata-export']))
    assert os.environ['APP_NAME'] == 'chronon_metadata_export'

    # If APP_NAME is set, should be respected.
    reset_env(default_environment)
    os.environ['APP_NAME'] = 'fake-name'
    run.set_runtime_env(parser.parse_args(args=['--mode', 'metadata-export']))
    assert os.environ['APP_NAME'] == 'fake-name'

    # If app_name can be passed from cli.
    reset_env(default_environment)
    run.set_runtime_env(parser.parse_args(args=['--mode', 'metadata-export', '--app-name', 'fake-name']))
    assert os.environ['APP_NAME'] == 'fake-name'

    # Check default backfill for a team sets parameters accordingly.
    reset_env(default_environment)
    run.set_runtime_env(parser.parse_args(args=[
        '--mode', 'backfill',
        '--conf', 'production/joins/sample_team/sample_online_join.v1',
        '--repo', repo
    ]))
    # from team env.
    assert os.environ['EXECUTOR_CORES'] == '4'
    # from default env.
    assert os.environ['DRIVER_MEMORY'] == '15G'
    # from common env.
    assert os.environ['VERSION'] == 'latest'
    # derived from args.
    assert os.environ['APP_NAME'] == 'chronon_joins_backfill_production_sample_team.sample_online_join.v1'

    # Check conf set environment overrides most.
    reset_env(default_environment)
    run.set_runtime_env(parser.parse_args(args=[
        '--mode', 'backfill',
        '--conf', 'production/joins/sample_team/sample_join.v1',
        '--repo', repo
    ]))
    # from conf env.
    assert os.environ['EXECUTOR_MEMORY'] == '9G'

    # Bad conf location raises error.
    with pytest.raises(Exception):
        reset_env(default_environment)
        run.set_runtime_env(parser.parse_args(args=[
            '--mode', 'backfill',
            '--conf', 'joins/sample_team/sample_join.v1',
            '--repo', repo
        ]))

