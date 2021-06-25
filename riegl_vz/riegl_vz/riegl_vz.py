import sys
import os
import time
import subprocess
import threading
import numpy as np
from os.path import join, dirname, abspath

import sensor_msgs.msg as sensor_msgs
import std_msgs.msg as std_msgs
import builtin_interfaces.msg as builtin_msgs

from rclpy.node import Node

import riegl.rdb

from vzi_services.controlservice import ControlService
from vzi_services.dataprocservice import DataprocService

from .ssh import RemoteClient
from .utils import (
    SubProcess
)

appDir = dirname(abspath(__file__))

class ScanPattern(object):
    def __init__(self):
        self.lineStart = 30.0
        self.lineStop = 130.0
        self.lineIncrement = 0.04
        self.frameStart = 0.0
        self.frameStop = 360.0
        self.frameIncrement = 0.04
        self.measProgram = 3

class RieglVz():
    def __init__(self, node):
        self.hostname = node.hostname
        self.sshUser = node.sshUser
        self.sshPwd = node.sshPwd
        self.workingDir = node.workingDir
        self._node = node
        self._logger = node.get_logger()
        self._connectionString = self.hostname + ":20000"
        self._busy = False
        self._scanBusy = False
        if not os.path.exists(self.workingDir):
            os.mkdir(self.workingDir)

    def _downloadAndPublishScan(self):
        self._logger.info("Downloading RDBX..")
        procSvc = DataprocService(self._connectionString)
        scanId = procSvc.actualFile(0)
        self._logger.debug("scan id = {}".format(scanId))
        rdbxFileRemote = "/media/" + scanId.replace(".rxp", ".rdbx")
        self._logger.debug("remote rdbx file = {}".format(rdbxFileRemote))
        rdbxFileLocal = self.workingDir + "/scan.rdbx"
        self._logger.debug("local rdbx file  = {}".format(rdbxFileLocal))
        ssh = RemoteClient(host=self.hostname, user=self.sshUser, password=self.sshPwd)
        ssh.download_file(filepath=rdbxFileRemote, localpath=rdbxFileLocal)
        ssh.disconnect()
        self._logger.info("RDBX download finished")

        self._logger.info("Extracting and publishing point cloud..")
        with riegl.rdb.rdb_open(self.rdbxFileLocal) as rdb:
            ts = builtin_msgs.Time(sec = 0, nanosec = 0)
            filter = ""
            rosDtype = sensor_msgs.PointField.FLOAT32
            dtype = np.float32
            itemsize = np.dtype(dtype).itemsize

            numTotalPoints = 0
            numPoints = 0
            data = bytearray()
            scanPublishLOD = self.scanPublishLOD
            if self.scanPublishLOD < 0:
                scanPublishLOD = 0
            for points in rdb.select(
                self.scanPublishFilter,
                chunk_size=100000
                ):
                pointStep = 2 ** scanPublishLOD
                for point in points:
                    if not (numTotalPoints % pointStep):
                        data.extend(point["riegl.xyz"].astype(dtype).tobytes())
                        data.extend(point["riegl.reflectance"].astype(dtype).tobytes())
                        numPoints += 1
                    numTotalPoints += 1

            fields = [sensor_msgs.PointField(
                name = n, offset = i*itemsize, datatype = rosDtype, count = 1)
                for i, n in enumerate('xyzr')]

            header = std_msgs.Header(frame_id = "RIEGL_SOCS", stamp = ts)

            pointCloud = sensor_msgs.PointCloud2(
                header = header,
                height = 1,
                width = numPoints,
                is_dense = False,
                is_bigendian = False,
                fields = fields,
                point_step = (itemsize * 4),
                row_step = (itemsize * 4 * numPoints),
                data = data
            )
            #for point in rdb.points():
            #    self._logger.debug("{0}".format(point.riegl_xyz))

            self._node.pointCloudPublisher.publish(pointCloud)

        self._logger.info("Point cloud published")

    def _scanThread(self):
        self._busy = True

        self._scanBusy = True
        self._logger.info("Starting data acquisition..")
        self._logger.info("project name = {}".format(self.projectName))
        self._logger.info("scanpos name = {}".format(self.scanposName))
        self._logger.info("storage media = {}".format(self.storageMedia))
        self._logger.info("scan pattern = {0}, {1}, {2}, {3}, {4}, {5}".format(
            self.scanPattern.lineStart,
            self.scanPattern.lineStop,
            self.scanPattern.lineIncrement,
            self.scanPattern.frameStart,
            self.scanPattern.frameStop,
            self.scanPattern.frameIncrement))
        self._logger.info("meas program = {}".format(self.scanPattern.measProgram))
        self._logger.info("scan publish = {}".format(self.scanPublish))
        self._logger.info("scan publish filter = '{}'".format(self.scanPublishFilter))
        self._logger.info("scan publish LOD = {}".format(self.scanPublishLOD))
        self._logger.info("scan register = {}".format(self.scanRegister))

        scriptPath = join(appDir, "acquire-data.py")
        cmd = [
            "python3", scriptPath,
            "--connectionstring", self._connectionString,
            "--project", self.projectName,
            "--scanposition", self.scanposName,
            "--storage-media", str(self.storageMedia)]
        if self.reflSearchSettings:
            rssFilepath = join(self.workingDir, "reflsearchsettings.json")
            with open(rssFilepath, "w") as f:
                json.dump(self.reflSearchSettings, f)
            cmd.append("--reflsearch")
            cmd.append(rssFilepath)
        if self.scanPattern:
            cmd.extend([
                "--line-start", str(self.scanPattern.lineStart),
                "--line-stop", str(self.scanPattern.lineStop),
                "--line-incr", str(self.scanPattern.lineIncrement),
                "--frame-start", str(self.scanPattern.frameStart),
                "--frame-stop", str(self.scanPattern.frameStop),
                "--frame-incr", str(self.scanPattern.frameIncrement),
                "--measprog", str(self.scanPattern.measProgram)
            ])
        if self.captureImages:
            cmd.extend([
                "--capture-images",
                "--capture-mode", str(self.captureMode),
                "--image-overlap", str(self.imageOverlap)
            ])
        self._logger.debug("CMD = {}".format(" ".join(cmd)))
        subproc = SubProcess(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        self._logger.debug("Subprocess started.")
        subproc.waitFor("Data acquisition failed.")
        self._logger.info("Data acquisition finished")
        self._scanBusy = False

        self._logger.info("Converting RXP to RDBX..")
        scriptPath = join(appDir, "create-rdbx.py")
        cmd = [
            "python3", scriptPath,
            "--connectionstring", self._connectionString,
            "--project", self.projectName,
            "--scanposition", self.scanposName]
        self._logger.debug("CMD = {}".format(" ".join(cmd)))
        subproc = SubProcess(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        self._logger.debug("Subprocess started.")
        subproc.waitFor("RXP to RDBX conversion failed.")
        self._logger.info("RXP to RDBX conversion finished")

        if self.scanPublish:
            self._downloadAndPublishScan()

        if self.scanRegister:
            print("Registering", flush=True)
            self._logger.info("Starting registration")
            scriptPath = os.path.join(appDir, "bin", "register-scan.py")
            cmd = [
                "python3", scriptPath,
                "--project", self.projectName,
                "--scanposition", self.scanposName]
            self._logger.debug("CMD = {}".format(" ".join(cmd)))
            subproc = SubProcess(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            subproc.waitFor("Registration failed.")
            self._logger.info("Registration finished")

        self._busy = False

    def scan(
        self,
        projectName: str,
        scanposName: str,
        storageMedia: int,
        scanPattern: ScanPattern,
        scanPublish: bool = True,
        scanPublishFilter: str = "",
        scanPublishLOD: int = 1,
        scanRegister: bool = True,
        reflSearchSettings: dict = None,
        captureImages: bool = False,
        captureMode: int = 1,
        imageOverlap: int = 25):
        """Acquire data at scan position.

        Args:
          projectName ... the project name
          scanposName ... the name of the new scan position
          storageMedia ... storage media for data recording
          scanPattern ... the scan pattern"""
        if self._busy:
            return False

        self.projectName = projectName
        self.scanposName = scanposName
        self.storageMedia = storageMedia
        self.scanPattern = scanPattern
        self.scanPublish = scanPublish
        self.scanPublishFilter = scanPublishFilter
        self.scanPublishLOD = scanPublishLOD
        self.scanRegister = scanRegister
        self.reflSearchSettings = reflSearchSettings
        self.captureImages = captureImages
        self.captureMode = captureMode
        self.imageOverlap = imageOverlap

        thread = threading.Thread(target=self._scanThread, args=())
        thread.daemon = True
        thread.start()

        return True

    def isScanBusy(self, block = True):
        if block:
            while self._scanBusy:
                time.sleep(0.2)
        return self._scanBusy

    def isBusy(self, block = True):
        if block:
            while self._busy:
                time.sleep(0.2)
        return self._busy

    def status(self):
        # tbd...
        return

    def stop(self):
        ctrlSvc = ControlService(self._connectionString)
        ctrlSvc.stop()
        isBusy()

    def shutdown(self):
        stop()
        scnSvc = ScannerService(self._connectionString)
        scnSvc.shutdown()
