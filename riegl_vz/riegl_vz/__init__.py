import sys
from datetime import datetime
import numpy as np
from std_srvs.srv import (
    Trigger,
    SetBool
)
from sensor_msgs.msg import (
    PointCloud2
)
import rclpy
from rclpy.node import Node
from rclpy.logging import LoggingSeverity
from .riegl_vz import (
    ScanPattern,
    RieglVz
)
from .utils import (
    SubProcess
)

_rieglVzWrapper = None

class RieglVzWrapper(Node):

    def __init__(self):
        super().__init__('riegl_vz')

        self._shutdownReq = False
        self._nextScanpos = 1

        self.declare_parameter('hostname', 'H2222222')
        self.declare_parameter('working_dir', '/tmp/ros_riegl_vz')
        self.declare_parameter('ssh_user', 'user')
        self.declare_parameter('ssh_password', 'user')
        self.declare_parameter('project_name', '')
        self.declare_parameter('storage_media', 2)
        self.declare_parameter('scan_pattern', [30.0,130.0,0.04,0.0,360.0,0.5])
        self.declare_parameter('meas_program', 0)
        self.declare_parameter('scan_publish', True)
        self.declare_parameter('scan_publish_filter', '')
        self.declare_parameter('scan_publish_lod', 0)
        self.declare_parameter('scan_register', True)

        self.hostname = str(self.get_parameter('hostname').value)
        self.workingDir = str(self.get_parameter('working_dir').value)
        self.sshUser = str(self.get_parameter('ssh_user').value)
        self.sshPwd = str(self.get_parameter('ssh_password').value)
        self.get_logger().debug("hostname = {}".format(self.hostname))
        self.get_logger().debug("workingDir = {}".format(self.workingDir))
        self.get_logger().debug("sshUser = {}".format(self.sshUser))
        self.get_logger().debug("sshPwd = {}".format(self.sshPwd))

        self.projectName = str(self.get_parameter('project_name').value)
        if not self.projectName:
            now = datetime.now()
            self._projectName = now.strftime("%y%m%d_%H%M%S")
        self.get_logger().debug("projectName = {}".format(self._projectName))

        self.pointCloudPublisher = self.create_publisher(PointCloud2, 'pointcloud', 2)

        self._setProjectService = self.create_service(Trigger, 'set_project', self._setProjectCallback)
        self._scanService = self.create_service(Trigger, 'scan', self._scanCallback)
        self._isBusyService = self.create_service(SetBool, 'is_scan_busy', self._isBusyCallback)
        self._isBusyService = self.create_service(SetBool, 'is_busy', self._isBusyCallback)
        self._stopService = self.create_service(Trigger, 'stop', self._stopCallback)
        self._shutdownService = self.create_service(Trigger, 'shutdown', self._shutdownCallback)

        self._rieglVz = RieglVz(self)

        self.get_logger().info("RIEGL VZ is now started, ready to get commands. (host = {}).".format(self.hostname))

    def setProject(self):
        self.projectName = str(self.get_parameter('project_name').value)

        if not self.projectName:
            now = datetime.now()
            self._projectName = now.strftime("%y%m%d_%H%M%S")

        self._nextScanpos = 1

    def _setProjectCallback(self, request, response):
        if self._shutdownReq is True:
            response.success = False
            response.message = "RIEGL VZ is shutting down"
            return response

        self.setProject()

        response.success = True
        response.message = "success"

        return response

    def scan(self):
        self.storageMedia = int(self.get_parameter('storage_media').value)
        scanPattern = self.get_parameter('scan_pattern').value
        self.scanPattern = ScanPattern()
        self.scanPattern.lineStart = scanPattern[0]
        self.scanPattern.lineStop = scanPattern[1]
        self.scanPattern.lineIncrement = scanPattern[2]
        self.scanPattern.frameStart = scanPattern[3]
        self.scanPattern.frameStop = scanPattern[4]
        self.scanPattern.frameIncrement = scanPattern[5]
        self.measProgram = int(self.get_parameter('meas_program').value)
        self.scanPublish = bool(self.get_parameter('scan_publish').value)
        self.scanPublishFilter = str(self.get_parameter('scan_publish_filter').value)
        self.scanPublishLOD = int(self.get_parameter('scan_publish_lod').value)
        self.scanRegister = bool(self.get_parameter('scan_register').value)

        self._scanposName = str(self._nextScanpos)
        self._nextScanpos = self._nextScanpos + 1

        return self._rieglVz.scan(
            projectName = self._projectName,
            scanposName = self._scanposName,
            storageMedia = self.storageMedia,
            scanPattern = self.scanPattern,
            scanPublishFilter = self.scanPublishFilter,
            scanPublish = self.scanPublish,
            scanPublishLOD = self.scanPublishLOD,
            scanRegister = self.scanRegister,
            reflSearchSettings = None,
            captureImages = False,
            captureMode = 1,
            imageOverlap = 25)

    def _scanCallback(self, request, response):
        if self._shutdownReq is True:
            response.success = False
            response.message = "RIEGL VZ is shutting down"
            return response

        if not self.scan():
            response.success = False
            response.message = "RIEGL VZ is busy"
            return response

        response.success = True
        response.message = "success"

        return response

    def isBusy(self):
        return self._rieglVz.isBusy()

    def _isBusyCallback(self, request, response):
        if self._shutdownReq is True:
            response.success = False
            response.message = "RIEGL VZ is shutting down"
            return response

        if not self.isBusy():
            response.success = False
            response.message = "RIEGL VZ is not busy"
            return response

        response.success = True
        response.message = "RIEGL VZ is busy"

        return response

    def isScanBusy(self):
        return self._rieglVz.isBusy()

    def _isScanBusyCallback(self, request, response):
        if self._shutdownReq is True:
            response.success = False
            response.message = "RIEGL VZ is shutting down"
            return response

        if not self.isScanBusy():
            response.success = False
            response.message = "RIEGL VZ is not busy"
            return response

        response.success = True
        response.message = "RIEGL VZ is busy"

        return response

    def stop(self):
        self._rieglVz.stop()

    def _stopCallback(self, request, response):
        if self._shutdownReq is True:
            response.success = False
            response.message = "RIEGL VZ is shutting down"
            return response

        self.stop()

        response.success = True
        response.message = "RIEGL VZ has been stopped"

        return response

    def shutdown(self):
        self._shutdownReq = True
        self.stop()
        self.rieglVZ.shutdown()

    def _shutdownCallback(self, request, response):
        self.shutdown()

        response.success = True
        response.message = "RIEGL VZ scanner is shutting down"

        return response

def stop_node():
    if _rieglVzWrapper is not None:
        _rieglVzWrapper.stop()
    rclpy.shutdown()

def main(args=None):
    rclpy.init(args=args)
    _rieglVzWrapper = RieglVzWrapper()
    try:
        rclpy.spin(_rieglVzWrapper)
    except KeyboardInterrupt:
        pass
    finally:
        stop_node()

if __name__ == "__main__":
    try:
        signal.signal(signal.SIGINT, stop_node)
        signal.signal(signal.SIGTERM, stop_node)
        main()
    except:
        stop_node()
