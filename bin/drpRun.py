#!/usr/bin/env python

# LSST Data Management System
# Copyright 2008, 2009, 2010, 2011 LSST Corporation.
# 
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the LSST License Statement and 
# the GNU General Public License along with this program.  If not, 
# see <http://www.lsstcorp.org/LegalNotices/>.

from __future__ import with_statement
from email.mime.text import MIMEText
import glob
from optparse import OptionParser
import os
import re
import socket
import sqlite
import subprocess
import sys
import tempfile
import time

import eups
# import lsst.pex.policy as pexPolicy

def _checkReadable(path):
    if not os.access(path, os.R_OK):
        raise RuntimeError("Required path " + path + " is unreadable")

def _checkWritable(path):
    if not os.access(path, os.W_OK):
        raise RuntimeError("Required path " + path + " is unwritable")

class NoMatchError(RuntimeError):
    pass

class RunConfiguration(object):

    ###########################################################################
    # Configuration information
    ###########################################################################

    spacePerCcd = int(160e6) # calexp only
    collection = "PT1.2"
    inputBase = "/lsst3/weekly/data"
    # outputBase = "/lsst3/weekly/datarel-runs"
    outputBase = "/home/jbosch/datarel-runs"
    lockBase = os.path.join(outputBase, "locks")
    pipelinePolicy = "PT1Pipe/main-ImSim.paf"
    runIdPattern = "%(runType)s_%(datetime)s"
    # toAddress = "lsst-devel-runs@lsstcorp.org"
    toAddress = "ktl@slac.stanford.edu"
    pipeQaBase = "http://lsst1.ncsa.illinois.edu/pipeQA/dev/"
    pipeQaDir = "/lsst/public_html/pipeQA/html/dev"
    dbHost = "lsst10.ncsa.uiuc.edu"

    # One extra process will be used on the first node for the JobOffice
    machineSets = {
            'rh5-1': ['lsst5:3', 'lsst11:2'],
            'rh5-2': ['lsst6:2', 'lsst8:2', 'lsst11:1'],
            # lsst5 8 cores 8 GB RH5
            # lsst6 8 cores 8 GB Condor RH5
            # lsst8 8 cores 8 GB ActiveMQ RH5
            # lsst11 4 cores 8 GB CentOS5
            'rh6-1': ['lsst9:3', 'lsst14:2'],
            'rh6-2': ['lsst14:1', 'lsst15:3'],
            # lsst9 8 cores 8 GB RH6
            # lsst14 4 cores 8 GB RH6
            # lsst15 4 cores 8 GB RH6
    }

    ###########################################################################

    def __init__(self, args):
        self.datetime = time.strftime("%Y_%m%d_%H%M%S")
        self.user = os.getlogin()
        self.hostname = socket.getfqdn()
        self.fromAddress = "%s@%s" % (self.user, self.hostname)

        self.options, self.args = self.parseOptions(args)

        # Handle immediate commands
        if self.options.printStatus:
            self.printStatus()
            sys.exit(0)
        if self.options.report is not None:
            self.report(os.path.join(self.options.output,
                self.options.report, "run", "run.log"))
            sys.exit(0)
        if self.options.errorReport is not None:
            print self.errorReport(self.options.errorReport)
            sys.exit(0)
        if self.options.listInputs:
            self.listInputs()
            sys.exit(0)
        if self.options.linkLatest is not None:
            self.linkLatest(self.options.linkLatest)
            sys.exit(0)
        if self.options.kill is not None:
            self.kill(self.options.kill)
            sys.exit(0)

        if self.arch is None:
            if self.options.arch is None:
                raise RuntimeError("Architecture is required")
            self.arch = self.options.arch

        if re.search(r'[^a-zA-Z0-9_]', self.options.runType):
            raise RuntimeError("Run type '%s' must be one word" %
                    (self.options.runType,))

        self.collectionName = re.sub(r'\.', '_', RunConfiguration.collection)
        runIdProperties = dict(
                user=self.user,
                coll=self.collectionName,
                runType=self.options.runType,
                datetime=self.datetime)
        self.runId = RunConfiguration.runIdPattern % runIdProperties
        runIdProperties['runid'] = self.runId
        dbNamePattern = "%(user)s_%(coll)s_u_%(runid)s"
        self.dbName = dbNamePattern % runIdProperties

        self.inputBase = os.path.join(RunConfiguration.inputBase,
                self.options.input)
        self.inputDirectory = os.path.join(self.inputBase,
                RunConfiguration.collection)
        self.outputDirectory = os.path.join(self.options.output, self.runId)
        self.outputDirectory = abspath(self.outputDirectory)
        if os.path.exists(self.outputDirectory):
            raise RuntimeError("Output directory %s already exists" %
                    (self.outputDirectory,))
        os.mkdir(self.outputDirectory)
        self.pipeQaUrl = RunConfiguration.pipeQaBase + self.dbName + "/"

        self.eupsPath = os.environ['EUPS_PATH']
        e = eups.Eups(readCache=False)
        self.setups = dict()
        for product in e.getSetupProducts():
            if product.name != "eups":
                self.setups[product.name] = product.version

        # TODO -- load policy and apply overrides
        self.options.override = None

    def printStatus(self):
        machineSets = RunConfiguration.machineSets.keys()
        machineSets.sort()
        for k in machineSets:
            lockFile = self._lockName(k)
            if os.path.exists(lockFile):
                print "*** Machine set", k, str(RunConfiguration.machineSets[k])
                self.report(lockFile)

    def report(self, logFile):
        with open(logFile, "r") as f:
            for line in f:
                print line,
                if line.startswith("Run:"):
                    runId = re.sub(r'Run:\s+', "", line.rstrip())
                if line.startswith("Output:"):
                    outputDir = re.sub(r'Output:\s+', "", line.rstrip())
        e = eups.Eups()
        if not e.isSetup("daf_persistence") or not e.isSetup("mysqlpython"):
            print >>sys.stderr, "*** daf_persistence and mysqlpython not setup, skipping log analysis"
        else:
            print self.orcaStatus(runId, outputDir)

    def orcaStatus(self, runId, outputDir):
        result = ""
        tailLog = False
        try:
            status = self.analyzeLogs(runId, inProgress=True)
            result += status
            tailLog = (status == "No log entries yet\n")
        except NoMatchError:
            result += "\tDatabase not yet created\n"
            tailLog = True

        if tailLog:
            logFile = os.path.join(outputDir, "run", "unifiedPipeline.log")
            with open(logFile, "r") as log:
                log.seek(-500, 2)
                result += "(last 500 bytes)... " + log.read(500) + "\n"

        return result

    def listInputs(self):
        for path in sorted(os.listdir(RunConfiguration.inputBase)):
            if os.path.exists(os.path.join(RunConfiguration.inputBase, path,
                RunConfiguration.collection)):
                print path

    def check(self):
        for requiredPackage in ['ctrl_orca', 'datarel', 'astrometry_net_data']:
            if not self.setups.has_key(requiredPackage):
                raise RuntimeError(requiredPackage + " is not setup")
        if self.setups['astrometry_net_data'].find('imsim') == -1:
            raise RuntimeError("Non-imsim astrometry_net_data is setup")
        if not self.setups.has_key('testing_pipeQA'):
            print >>sys.stderr, "testing_pipeQA not setup, will skip pipeQA"
            self.options.doPipeQa = False
        if not self.setups.has_key('testing_displayQA'):
            print >>sys.stderr, "testing_displayQA not setup, will skip pipeQA"
            self.options.doPipeQa = False

        _checkReadable(self.inputDirectory)
        _checkReadable(os.path.join(self.inputDirectory, "bias"))
        _checkReadable(os.path.join(self.inputDirectory, "dark"))
        _checkReadable(os.path.join(self.inputDirectory, "flat"))
        _checkReadable(os.path.join(self.inputDirectory, "raw"))
        _checkReadable(os.path.join(self.inputDirectory, "refObject.csv"))
        self.registryPath = os.path.join(self.inputDirectory, "registry.sqlite3")
        _checkReadable(self.registryPath)

        if self.options.ccdCount is None:
            conn = sqlite.connect(self.registryPath)
            self.options.ccdCount = conn.execute(
                    """SELECT COUNT(DISTINCT visit||':'||raft||':'||sensor)
                    FROM raw;""").fetchone()[0]
        if self.options.ccdCount < 2:
            raise RuntimeError("Must process at least two CCDs")

        _checkWritable(self.outputDirectory)
        result = os.statvfs(self.outputDirectory)
        availableSpace = result.f_bavail * result.f_bsize
        minimumSpace = int(RunConfiguration.spacePerCcd * self.options.ccdCount)
        if availableSpace < minimumSpace:
            raise RuntimeError("Insufficient disk space in output filesystem:\n"
                    "%d available, %d needed" %
                    (availableSpace, minimumSpace))

    def run(self):
        self.runInfo = """Run: %s
RunType: %s
User: %s
Pipeline: %s
Input: %s
CCD count: %d
Output: %s
Database: %s
Overrides: %s
""" % (self.runId, self.options.runType, self.user, self.options.pipeline,
        self.options.input, self.options.ccdCount, self.outputDirectory,
        self.dbName, str(self.options.override))

        self.lockMachines()
        # TODO -- better error handling
        # on error, log problem, E-mail problem and relevant output, make sure
        # all resources are cleaned up
        try:
            os.chdir(self.outputDirectory)
            os.mkdir("run")
            os.chdir("run")
            self._log("Run directory created")
            self.generatePolicy()
            self._log("Policy created")
            self.generateInputList()
            self._log("Input list created")
            self.generateEnvironment()
            self._log("Environment created")
            self._sendmail("[drpRun] Start on %s: run %s" %
                    (self.machineSet, self.runId),self.runInfo)
            self._log("Orca run started")
            self.doOrcaRun()
            self._log("Orca run complete")
            self.setupCheck()
            self._sendmail("[drpRun] Orca done: run %s" % (self.runId,),
                    self.analyzeLogs(self.runId))
            self.doAdditionalJobs()
            self._log("SourceAssociation and ingest complete")
            if self.options.doPipeQa:
                self._sendmail("[drpRun] pipeQA start: run %s" %
                        (self.runId,), "pipeQA link: %s" % (self.pipeQaUrl,))
                self.doPipeQa()
                self._log("pipeQA complete")
            if not self.options.testOnly:
                self.doLatestLinks()
            if self.options.doPipeQa:
                self._sendmail("[drpRun] Complete: run %s" %
                        (self.runId,), "pipeQA link: %s " % (self.pipeQaUrl,))
            else:
                self._sendmail("[drpRun] Complete: run %s" %
                        (self.runId,), self.runInfo)
        finally:
            self.unlockMachines()


###############################################################################
# 
# General utilities
# 
###############################################################################

    def _sendmail(self, subject, body, toStderr=True):
        print >>sys.stderr, subject
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = self.fromAddress
        msg['To'] = self.options.toAddress

        mail = subprocess.Popen(["sendmail", "-t", "-f", self.fromAddress],
                stdin=subprocess.PIPE)
        try:
            print >>mail.stdin, msg
        finally:
            mail.stdin.close()

    def _lockName(self, machineSet):
        return os.path.join(RunConfiguration.lockBase, machineSet)

    def _log(self, message):
        with open(self._lockName(self.machineSet), "a") as lockFile:
            print >>lockFile, time.asctime(), message
        print >>sys.stderr, time.asctime(), message

###############################################################################
# 
# Generate input files
# 
###############################################################################

    def generatePolicy(self):
        with open("joboffice.paf", "w") as policyFile:
            print >>policyFile, """#<?cfg paf policy ?>
execute: {
  shutdownTopic: "workflowShutdown"
  eventBrokerHost: "lsst8.ncsa.uiuc.edu"
}
framework: {
  exec: "$DATAREL_DIR/pipeline/PT1Pipe/joboffice-ImSim.sh"
  type: "standard"
  environment: unused
}
"""

        with open("platform.paf", "w") as policyFile:
            print >>policyFile, """#<?cfg paf policy ?>
dir: {
    defaultRoot: """ + self.options.output + """
    runDirPattern:  "%(runid)s"
    work:     work
    input:    input
    output:   output
    update:   update
    scratch:  scr
}

hw: {
    nodeCount:  4
    minCoresPerNode:  2
    maxCoresPerNode:  8
    minRamPerNode:  2.0
    maxRamPerNode: 16.0
}

deploy:  {
    defaultDomain:  ncsa.illinois.edu
"""
            first = True
            for machine in RunConfiguration.machineSets[self.machineSet]:
                if first:
                    processes = int(re.sub(r'.*:', "", machine)) + 1
                    jobOfficeMachine = re.sub(r':.*', "", machine)
                    print >>policyFile, "            nodes: ", \
                            jobOfficeMachine + ":" + str(processes)
                    first = False
                else:
                    print >>policyFile, "            nodes: ", machine
            print >>policyFile, "}"

        subprocess.check_call(
                "cp $DATAREL_DIR/pipeline/%s ." % (self.options.pipeline,),
                shell=True)

        if self.options.pipeline.find("/") != -1:
            components = self.options.pipeline.split("/")
            dir = os.path.join(*components[0:-1])
            policy = components[-1]
            subprocess.check_call(
                    "ln -s $DATAREL_DIR/pipeline/%s ." % (dir,),
                    shell=True)
        else:
            policy = self.options.pipeline

        with open("orca.paf", "w") as policyFile:
            print >>policyFile, """#<?cfg paf policy ?>
shortName:           DataRelease
eventBrokerHost:     lsst8.ncsa.uiuc.edu
repositoryDirectory: .
productionShutdownTopic:       productionShutdown

database: {
    name: dc3bGlobal
    system: {   
        authInfo: {
            host: """ + RunConfiguration.dbHost + """
            port: 3306
        }
        runCleanup: {
            daysFirstNotice: 7  # days when first notice is sent before run can be deleted
            daysFinalNotice: 1  # days when final notice is sent before run can be deleted
        }
    }

    configurationClass: lsst.ctrl.orca.db.DC3Configurator
    configuration: {  
        globalDbName: GlobalDB
        dcVersion: """ + self.collectionName + """
        dcDbName: DC3b_DB
        minPercDiskSpaceReq: 10   # measured in percentages
        userRunLife: 2            # measured in weeks
    }
    logger: {
        launch: true
    }
}

workflow: {
    shortName: Workflow
    platform: @platform.paf
    shutdownTopic:       workflowShutdown

    configurationClass: lsst.ctrl.orca.GenericPipelineWorkflowConfigurator
    configuration: {
        deployData: {
            dataRepository: """ + self.inputBase + """
            collection: """ + RunConfiguration.collection + """
            script: "$DATAREL_DIR/bin/runOrca/deployData.sh"
        }
        announceData: {
            script: $CTRL_SCHED_DIR/bin/announceDataset.py
            topic: RawCcdAvailable
            inputdata: ./ccdlist
        }
    }

    pipeline: {
        shortName:     joboffices
        definition:    @joboffice.paf
        runCount: 1
        deploy: {
            processesOnNode: """ + jobOfficeMachine + """:1
        }
        launch: true
    }
"""
            self.nPipelines = 0
            for machine in RunConfiguration.machineSets[self.machineSet]:
                machineName, processes = machine.split(':')
                self.nPipelines += int(processes)
                print >>policyFile, """
    pipeline: {
        shortName:     """ + machineName + """
        definition:    @""" + policy + """
        runCount: """ + processes + """
        deploy: {
            processesOnNode: """ + machineName + ":" + processes + """
        }
        launch: true
    }
"""
            print >>policyFile, "}"

    def generateInputList(self):
        with open("ccdlist", "w") as inputFile:
            print >>inputFile, ">intids visit"
            conn = sqlite.connect(self.registryPath)
            conn.text_factory = str
            cmd = "SELECT DISTINCT visit, raft, sensor " + \
                    "FROM raw ORDER BY visit, raft, sensor"
            if self.options.ccdCount is not None and self.options.ccdCount > 0:
                cmd += " LIMIT %d" % (self.options.ccdCount,)
            cursor = conn.execute(cmd)
            for row in cursor:
                print >>inputFile, "raw visit=%s raft=%s sensor=%s" % row

            for i in xrange(self.nPipelines):
                print >>inputFile, "raw visit=0 raft=0 sensor=0"

    def generateEnvironment(self):
        with open("env.sh", "w") as envFile:
            # TODO -- change EUPS_PATH based on selected architecture
            print >>envFile, "export EUPS_PATH=" + self.eupsPath
            for dir in self.eupsPath.split(':'):
                loadScript = os.path.join(dir, "loadLSST.sh")
                if os.path.exists(loadScript):
                    print >>envFile, "source", loadScript
                    break
            for pkg in sorted(self.setups.keys()):
                print >>envFile, "setup -j", pkg, self.setups[pkg]

        configDirectory = os.path.join(self.outputDirectory, "config")
        os.mkdir(configDirectory)
        subprocess.check_call("eups list --setup > %s/weekly.tags" %
                (configDirectory,), shell=True)

###############################################################################
# 
# Routines for executing production
# 
###############################################################################

    def _lockSet(self, machineSet):
        (tempFileDescriptor, tempFilename) = \
                tempfile.mkstemp(dir=RunConfiguration.lockBase)
        with os.fdopen(tempFileDescriptor, "w") as tempFile:
            print >>tempFile, self.runInfo,
        os.chmod(tempFilename, 0644)
        try:
            os.link(tempFilename, self._lockName(machineSet))
        except:
            os.unlink(tempFilename)
            return False
        os.unlink(tempFilename)
        return True

    def lockMachines(self):
        machineSets = sorted(RunConfiguration.machineSets.keys())
        for machineSet in machineSets:
            if machineSet.startswith(self.arch):
                if self._lockSet(machineSet):
                    self.machineSet = machineSet
                    return
        raise RuntimeError("Unable to acquire a machine set for arch %s" %
                (self.arch,))

    def unlockMachines(self):
        os.rename(self._lockName(self.machineSet),
                os.path.join(self.outputDirectory, "run", "run.log"))

    def _exec(self, command, logFile):
        try:
            subprocess.check_call(command + " >& " + logFile, shell=True)
        except subprocess.CalledProcessError:
            cmd = command.split(' ', 1)[0].split('/')[-1]
            print >>sys.stderr, "***", cmd, "failed"
            with open(logFile, "r") as log:
                log.seek(-500, 2)
                print >>sys.stderr, "(last 500 bytes)...", log.read(500)
            raise

    def doOrcaRun(self):
        try:
            subprocess.check_call("$CTRL_ORCA_DIR/bin/orca.py"
                    " -e env.sh"
                    " -r ."
                    " -V 30 -L 2 orca.paf " + self.runId + 
                    " >& unifiedPipeline.log",
                    shell=True)
            # TODO -- monitor orca run, looking for output changes
            # TODO -- look for MemoryErrors and bad_allocs in logs
        except subprocess.CalledProcessError:
            print >>sys.stderr, "*** Orca failed"
            print >>sys.stderr, self.orcaStatus(self.runId,
                    self.outputDirectory)
            raise

    def setupCheck(self):
        tags = os.path.join(self.outputDirectory, "config", "weekly.tags")
        for env in glob.glob(os.path.join(self.outputDirectory,
            "work", "*", "eups-env.txt")):
            try:
                subprocess.check_call(["diff", env, tags])
            except subprocess.CalledProcessError:
                print >>sys.stderr, "*** Mismatched setup", env
                raise

    def doAdditionalJobs(self):
        os.mkdir("../SourceAssoc")

        self._exec("$DATAREL_DIR/bin/sst/SourceAssoc_ImSim.py"
                " -i ../update"
                " -o ../SourceAssoc"
                " -R ../update/registry.sqlite3",
                "SourceAssoc_ImSim.log")
        self._log("SourceAssoc complete")
        self._exec("$DATAREL_DIR/bin/ingest/prepareDb.py"
                " -u %s -H %s %s" %
                (self.user, RunConfiguration.dbHost, self.dbName),
                "prepareDb.log")
        self._log("prepareDb complete")

        os.chdir("..")
        self._exec("$DATAREL_DIR/bin/ingest/ingestProcessed_ImSim.py"
                " -u %s -d %s"
                " update update/registry.sqlite3" %
                (self.user, self.dbName),
                "run/ingestProcessed_ImSim.log")
        os.chdir("run")
        self._log("ingestProcessed complete")
        
        os.mkdir("../csv-SourceAssoc")
        self._exec("$DATAREL_DIR/bin/ingest/ingestSourceAssoc.py"
                " -m"
                " -u %s -H %s"
                " -R ../input/refObject.csv"
                " -e ../Science_Ccd_Exposure_Metadata.csv"
                " -j 1"
                " %s ../SourceAssoc ../csv-SourceAssoc" %
                (self.user, RunConfiguration.dbHost, self.dbName),
                "ingestSourceAssoc.log")
        self._log("ingestSourceAssoc complete")
        self._exec("$DATAREL_DIR/bin/ingest/ingestSdqa_ImSim.py"
                " -u %s -H %s -d %s"
                " ../update ../update/registry.sqlite3" %
                (self.user, RunConfiguration.dbHost, self.dbName),
                "ingestSdqa_ImSim.log")
        self._log("ingestSdqa complete")
        self._exec("$DATAREL_DIR/bin/ingest/finishDb.py"
                " -u %s -H %s"
                " -t"
                " %s" %
                (self.user, RunConfiguration.dbHost, self.dbName),
                "finishDb.log")
        self._log("finishDb complete")

    def doPipeQa(self):
        _checkWritable(RunConfiguration.pipeQaDir)
        os.environ['WWW_ROOT'] = RunConfiguration.pipeQaDir
        self._exec("$TESTING_DISPLAYQA_DIR/bin/newQa.py " + self.dbName,
                "newQa.log")
        self._exec("$TESTING_PIPEQA_DIR/bin/pipeQa.py"
                " --delaySummary"
                " --forkFigure"
                " --keep"
                " --breakBy ccd"
                " " + self.dbName,
                "pipeQa.log")

    def linkLatest(self, runId):
        self.outputDirectory = os.path.join(self.options.output, runId)
        _checkReadable(self.outputDirectory)
        with open(os.path.join(self.outputDirectory, "run", "run.log")) as logFile:
            for line in logFile:
                if line.startswith("RunType:"):
                    self.options.runType = re.sub(r'^RunType:\s+', "",
                            line.rstrip())
                if line.startswith("Database:"):
                    self.dbName = re.sub(r'^Database:\s+', "", line.rstrip())
        self.doLatestLinks()

    def doLatestLinks(self):
        # TODO -- remove race conditions
        _checkWritable(self.options.output)
        latest = os.path.join(self.options.output,
                "latest_" + self.options.runType)
        if os.path.exists(latest + ".bak"):
            os.unlink(latest + ".bak")
        if os.path.exists(latest):
            os.rename(latest, latest + ".bak")
        os.symlink(self.outputDirectory, latest)
# TODO -- linkDb.py needs to be extended to take more run types
#        self._exec("$DATAREL_DIR/bin/ingest/linkDb.py"
#                " -u %s -H %s"
#                " -t %s"
#                " %s" % (self.user, RunConfiguration.dbHost,
#                    self.options.runType, self.dbName), "linkDb.log")
        latest = os.path.join(RunConfiguration.pipeQaDir,
                "latest_" + self.options.runType)
        qaDir = os.path.join(RunConfiguration.pipeQaDir, self.dbName)
        if os.path.exists(qaDir):
            if os.path.exists(latest + ".bak"):
                os.unlink(latest + ".bak")
            if os.path.exists(latest):
                os.rename(latest, latest + ".bak")
            os.symlink(qaDir, latest)

    def findMachineSet(self, runId):
        for lockFileName in os.listdir(RunConfiguration.lockBase):
            with open(os.path.join(RunConfiguration.lockBase, lockFileName),
                    "r") as lockFile:
                for line in lockFile:
                    if line == "Run: " + runId + "\n":
                        return os.path.basename(lockFileName)
        return None

    def kill(self, runId):
        e = eups.Eups()
        if not e.isSetup("ctrl_orca"):
            print >>sys.stderr, "ctrl_orca not setup, using default version"
            e.setup("ctrl_orca")
        self.machineSet = self.findMachineSet(runId)
        if self.machineSet is None:
            raise RuntimeError("No current run with runId " + runId)
        self._log("orca killed")
        subprocess.check_call("$CTRL_ORCA_DIR/bin/shutprod.py 1 " + runId,
                shell=True)

###############################################################################
# 
# Analyze logs during/after run
# 
###############################################################################

    def analyzeLogs(self, runId, inProgress=False):
        import MySQLdb
        from lsst.daf.persistence import DbAuth
        jobStartRegex = re.compile(
                r"Processing job: type=calexp "
                "sensor=(?P<sensor>\d,\d) "
                "visit=(?P<visit>\d+) "
                "raft=(?P<raft>\d,\d)"
        )

        host = RunConfiguration.dbHost
        port = 3306
        with MySQLdb.connect(
                host=host,
                port=port,
                user=DbAuth.username(host, str(port)),
                passwd=DbAuth.password(host, str(port))) as conn:
            runpat = '%' + runId + '%'
            conn.execute("SHOW DATABASES LIKE %s", (runpat,))
            ret = conn.fetchall()
            if ret is None or len(ret) == 0:
                raise NoMatchError("No match for run %s" % (runId,))
            elif len(ret) > 1:
                raise RuntimeError("Multiple runs match:\n" +
                        str([r[0] for r in ret]))
            dbName = ret[0][0]

        result = ""
        try:
            conn = MySQLdb.connect(
                host=host,
                port=port,
                user=DbAuth.username(host, str(port)),
                passwd=DbAuth.password(host, str(port)),
                db=dbName)

            cursor = conn.cursor()
            cursor.execute("""SELECT TIMESTAMP, timereceived FROM Logs
                WHERE id = (SELECT MIN(id) FROM Logs)""")
            row = cursor.fetchone()
            if row is None:
                if inProgress:
                    return "No log entries yet\n"
                else:
                    return "*** No log entries written\n"
            startTime, start = row
            result += "First log entry: %s\n" % (start,)
    
            cursor = conn.cursor()
            cursor.execute("""SELECT TIMESTAMP, timereceived FROM Logs
                WHERE id = (SELECT MAX(id) FROM Logs)""")
            stopTime, stop = cursor.fetchone()
            result += "Last log entry: %s\n" % (stop,)
            elapsed = long(stopTime) - long(startTime)
            elapsedHr = elapsed / 3600 / 1000 / 1000 / 1000
            elapsed -= elapsedHr * 3600 * 1000 * 1000 * 1000
            elapsedMin = elapsed / 60 / 1000 / 1000 / 1000
            elapsed -= elapsedMin * 60 * 1000 * 1000 * 1000
            elapsedSec = elapsed / 1.0e9
            result += "Elapsed time: %d:%02d:%06.3f\n" % (elapsedHr,
                    elapsedMin, elapsedSec)
    
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(DISTINCT workerid) FROM
                    (SELECT workerid FROM Logs LIMIT 10000) AS sample""")
            nPipelines = cursor.fetchone()[0]
            result += "%d pipelines used\n" % (nPipelines,)
    
            cursor = conn.cursor()
            cursor.execute("""
                SELECT CASE gid
                    WHEN 1 THEN 'pipeline shutdowns seen'
                    WHEN 2 THEN 'CCDs attempted'
                    WHEN 3 THEN 'src writes'
                    WHEN 4 THEN 'calexp writes'
                END AS descr, COUNT(*) FROM (
                    SELECT CASE
                        WHEN COMMENT LIKE 'Processing job:% visit=0 %'
                        THEN 1
                        WHEN COMMENT LIKE 'Processing job:%'
                            AND COMMENT NOT LIKE '% visit=0 %'
                        THEN 2
                        WHEN COMMENT LIKE 'Ending write to BoostStorage%/src%'
                        THEN 3
                        WHEN COMMENT LIKE 'Ending write to FitsStorage%/calexp%'
                        THEN 4
                        ELSE 0
                    END AS gid
                    FROM Logs
                ) AS stats WHERE gid > 0 GROUP BY gid""")
            nShutdown = 0
            for d, n in cursor.fetchall():
                result += "%d %s\n" % (n, d)
                if d == 'pipeline shutdowns seen':
                    nShutdown = n
            if nShutdown != nPipelines:
                if not inProgress:
                    if nShutdown == 0:
                        result += "\n*** No pipelines were shut down properly\n"
                    else:
                        result += "\n*** Shutdowns do not match pipelines\n"
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT workerid, COMMENT
                    FROM Logs JOIN
                    (SELECT MAX(id) AS last FROM Logs GROUP BY workerid) AS a
                    ON (Logs.id = a.last)""")
                for worker, msg in cursor.fetchall():
                    if inProgress:
                        result += "Pipeline %s last status: %s\n" % (worker,
                                msg)
                    else:
                        result += "Pipeline %s ended with: %s\n" % (worker, msg)
    
            cursor = conn.cursor()
            cursor.execute("""
SELECT COUNT(*) FROM Logs
WHERE
(
    	COMMENT LIKE '%rror%'
	OR COMMENT LIKE '%xception%'
	OR COMMENT LIKE '%arning%'
	OR COMMENT LIKE 'Fail'
	OR COMMENT LIKE 'fail'
)
AND COMMENT NOT LIKE '%failureStage%'
AND COMMENT NOT LIKE '%failure stage%'
AND COMMENT NOT LIKE 'failSerialName%'
AND COMMENT NOT LIKE 'failParallelName%'
AND COMMENT NOT LIKE 'Distortion fitter failed to improve%'
AND COMMENT NOT LIKE '%magnitude error column%'
AND COMMENT NOT LIKE '%errorFlagged%'
AND COMMENT NOT LIKE 'Skipping process due to error'
            """)
            result += "%s failures seen\n" % cursor.fetchone()

        finally:
            conn.close()
        return result

    def errorReport(self, runId):
        import MySQLdb
        from lsst.daf.persistence import DbAuth
        jobStartRegex = re.compile(
                r"Processing job: type=calexp "
                "sensor=(?P<sensor>\d,\d) "
                "visit=(?P<visit>\d+) "
                "raft=(?P<raft>\d,\d)"
        )

        host = RunConfiguration.dbHost
        port = 3306
        with MySQLdb.connect(
                host=host,
                port=port,
                user=DbAuth.username(host, str(port)),
                passwd=DbAuth.password(host, str(port))) as conn:
            runpat = '%' + runId + '%'
            conn.execute("SHOW DATABASES LIKE %s", (runpat,))
            ret = conn.fetchall()
            if ret is None or len(ret) == 0:
                raise NoMatchError("No match for run %s" % (runId,))
            elif len(ret) > 1:
                raise RuntimeError("Multiple runs match:\n" +
                        str([r[0] for r in ret]))
            dbName = ret[0][0]

        result = ""
        try:
            conn = MySQLdb.connect(
                host=host,
                port=port,
                user=DbAuth.username(host, str(port)),
                passwd=DbAuth.password(host, str(port)),
                db=dbName)

            cursor = conn.cursor()
            cursor.execute("""SELECT TIMESTAMP, timereceived FROM Logs
                WHERE id = (SELECT MIN(id) FROM Logs)""")
            row = cursor.fetchone()
            if row is None:
                return "*** No log entries written\n"

            cursor = conn.cursor()
            cursor.execute("""
                SELECT workerid, COMMENT
                FROM Logs JOIN
                (SELECT MAX(id) AS last FROM Logs GROUP BY workerid) AS a
                ON (Logs.id = a.last)""")
            for worker, msg in cursor.fetchall():
                result += "Pipeline %s last status: %s\n" % (worker, msg)

            cursor = conn.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute("""
                SELECT * FROM Logs
                WHERE COMMENT LIKE 'Processing job:%'
                    OR (
                        (
                            COMMENT LIKE '%rror%'
                            OR COMMENT LIKE '%xception%'
                            OR COMMENT LIKE '%arning%'
                            OR COMMENT LIKE '%Fail%'
                            OR COMMENT LIKE '%fail%'
                        )
                        AND COMMENT NOT LIKE '%failureStage%'
                        AND COMMENT NOT LIKE '%failure stage%'
                        AND COMMENT NOT LIKE 'failSerialName%'
                        AND COMMENT NOT LIKE 'failParallelName%'
                        AND COMMENT NOT LIKE 'Distortion fitter failed to improve%'
                        AND COMMENT NOT LIKE '%magnitude error column%'
                        AND COMMENT NOT LIKE '%errorFlagged%'
                        AND COMMENT NOT LIKE 'Skipping process due to error'
                    )
                ORDER BY id;""")
            jobs = dict()
            for d in cursor.fetchall():
                match = jobStartRegex.search(d['COMMENT'])
                if match:
                    jobs[d['workerid']] = "Visit %s Raft %s Sensor %s" % (
                            match.group("visit"), match.group("raft"),
                            match.group("sensor"))
                elif not d['COMMENT'].startswith('Processing job:'):
                    if jobs.has_key(d['workerid']):
                        result += "*** Error in %s on %s:\n" % (
                                jobs[d['workerid']], d['workerid'])
                    else:
                        result += "*** Error in unknown job on %s:\n" % (
                                d['workerid'],)
                    lines = d['COMMENT'].split('\n')
                    i = -1
                    message = lines[i].strip()
                    while message == "":
                        i -= 1
                        message = lines[i].strip()
                    result += lines[i-1].strip() + "\n" + message + "\n"


        finally:
            conn.close()
        return result

###############################################################################
# 
# Parse command-line options
# 
###############################################################################

    def parseOptions(self, args):
        parser = OptionParser("""%prog [options]

Perform an integrated production run.

Uses the current stack and setup package versions.""")

        parser.add_option("-t", "--runType", metavar="WORD",
                help="one-word ('_' allowed) description of run type (default: %default)")

        parser.add_option("-p", "--pipeline", metavar="PAF",
                help="master pipeline policy in DATAREL_DIR/pipeline"
                " (default: %default)")
        # TODO -- allow overrides of policy parameters
        # parser.add_option("-D", "--define", dest="override",
        #         metavar="KEY=VALUE",
        #         action="append",
        #         help="overrides for policy items (repeatable)")
        
        archs = set()
        self.arch = None
        machineName = self.hostname.split('.')[0]
        for machineSet, machines in RunConfiguration.machineSets.iteritems():
            a = re.sub(r'-.*', "", machineSet)
            archs.add(a)
            for machine in machines:
                if machineName == re.sub(r':.*', "", machine):
                    self.arch = a
                    break
        archs = sorted(list(archs))

        if self.arch is None:
            parser.add_option("-a", "--arch", type="choice",
                    choices=archs,
                    help="machine architecture [" + ', '.join(archs) + "]")

        parser.add_option("-S", "--status", dest="printStatus",
                action="store_true",
                help="print current run status and exit")
        parser.add_option("-R", "--report", metavar="RUNID",
                help="print report for RUNID and exit")
        parser.add_option("-E", "--errorReport", metavar="RUNID",
                help="print error report for RUNID and exit")
        parser.add_option("-k", "--kill", metavar="RUNID",
                help="kill Orca processes and exit")
        
        parser.add_option("-i", "--input", metavar="DIR",
                help="input dataset path (default: %default)")
        parser.add_option("-I", "--listInputs", action="store_true",
                help="list available official inputs and exit")
        parser.add_option("-n", "--ccdCount", metavar="N", type="int",
                help="run only first N CCDs (default: all)")

        parser.add_option("-o", "--output", metavar="DIR",
                help="output dataset base path (default: %default)")

        parser.add_option("-x", "--testOnly", action="store_true",
                help="do NOT link run results as the latest of its type")
        parser.add_option("-L", "--linkLatest", metavar="RUNID",
                help="link previous run result as the latest of its type and exit")
        parser.add_option("--skipPipeQa", dest="doPipeQa",
                action="store_false",
                help="skip running pipeQA")

        parser.add_option("-m", "--mail", dest="toAddress",
                metavar="ADDR",
                help="E-mail address for notifications (default: %default)")

        input = None
        for entry in sorted(os.listdir(RunConfiguration.inputBase),
                reverse=True):
            if entry.startswith("obs_imSim"):
                input = entry
                break

        parser.set_defaults(
                runType=self.user,
                pipeline=RunConfiguration.pipelinePolicy,
                input=input,
                output=RunConfiguration.outputBase,
                doPipeQa=True,
                toAddress=RunConfiguration.toAddress)

        return parser.parse_args(args)

def main():
    configuration = RunConfiguration(sys.argv)
    configuration.check()
    configuration.run()

if __name__ == "__main__":
    main()
