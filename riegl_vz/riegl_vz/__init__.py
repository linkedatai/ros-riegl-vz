import sys
from datetime import datetime
import numpy as np

from std_srvs.srv import (
    Trigger,
    SetBool
)
from sensor_msgs.msg import (
    PointCloud2,
    NavSatFix
)
from geometry_msgs.msg import (
    PoseStamped,
    TransformStamped
)
from nav_msgs.msg import (
    Path,
    Odometry
)
from diagnostic_msgs.msg import (
    DiagnosticArray,
    DiagnosticStatus
)
from diagnostic_updater import Updater
from tf2_ros import TransformBroadcaster
from riegl_vz_interfaces.srv import (
    GetPointCloud,
    GetScanPoses,
    GetPose,
    SetPosition,
    GetScanPatterns
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

        self.declare_parameter('hostname', 'H2222222')
        self.declare_parameter('working_dir', '/tmp/ros_riegl_vz')
        self.declare_parameter('ssh_user', 'user')
        self.declare_parameter('ssh_password', 'user')
        self.declare_parameter('project_name', '')
        self.declare_parameter('storage_media', 0)
        self.declare_parameter('scan_pattern', [30.0,130.0,0.04,0.0,360.0,0.5])
        self.declare_parameter('meas_program', 0)
        self.declare_parameter('scan_publish', True)
        self.declare_parameter('scan_publish_filter', '')
        self.declare_parameter('scan_publish_lod', 0)
        self.declare_parameter('scan_register', True)
        self.declare_parameter('reflector_search', False)
        self.declare_parameter('reflector_search_models', '')
        self.declare_parameter('reflector_search_limits', [0.0, 10000.0])
        self.declare_parameter('control_points_csv_file', '')
        self.declare_parameter('control_points_coord_system', '')

        self.hostname = str(self.get_parameter('hostname').value)
        self.workingDir = str(self.get_parameter('working_dir').value)
        self.sshUser = str(self.get_parameter('ssh_user').value)
        self.sshPwd = str(self.get_parameter('ssh_password').value)
        self.projectName = str(self.get_parameter('project_name').value)
        self.storageMedia = int(self.get_parameter('storage_media').value)
        self.get_logger().debug("hostname = {}".format(self.hostname))
        self.get_logger().debug("workingDir = {}".format(self.workingDir))
        self.get_logger().debug("sshUser = {}".format(self.sshUser))
        self.get_logger().debug("sshPwd = {}".format(self.sshPwd))
        self.get_logger().debug("projectName = {}".format(self.projectName))
        self.get_logger().debug("storageMedia = {}".format(self.storageMedia))

        self.scanPublishFilter = str(self.get_parameter('scan_publish_filter').value)
        self.get_logger().debug("scanPublishFilter = {}".format(self.scanPublishFilter))
        self.scanPublishLOD = int(self.get_parameter('scan_publish_lod').value)
        self.get_logger().debug("scanPublishLOD = {}".format(self.scanPublishLOD))

        self.pointCloudPublisher = self.create_publisher(PointCloud2, 'pointcloud', 2)
        self.posePublisher = self.create_publisher(PoseStamped, 'pose', 10)
        self.pathPublisher = self.create_publisher(Path, 'path', 10)
        self.odomPublisher = self.create_publisher(Odometry, 'odom', 10)
        self.gnssFixPublisher = self.create_publisher(NavSatFix, 'gnss/fix', 10)
        self.scanGnssFixPublisher = self.create_publisher(NavSatFix, 'gnss/fix/scan', 10)

        self.transformBroadcaster = TransformBroadcaster(self)

        self._setProjectService = self.create_service(Trigger, 'set_project', self._setProjectCallback)
        self._scanService = self.create_service(Trigger, 'scan', self._scanCallback)
        self._getPointCloudService = self.create_service(GetPointCloud, 'get_pointcloud', self._getPointCloudCallback)
        self._setPositionService = self.create_service(SetPosition, 'set_position', self._setPositionCallback)
        self._getSopvService = self.create_service(GetPose, 'get_sopv', self._getSopvCallback)
        self._getVopService = self.create_service(GetPose, 'get_vop', self._getVopCallback)
        self._getPopService = self.create_service(GetPose, 'get_pop', self._getPopCallback)
        self._getScanPoses = self.create_service(GetScanPoses, 'get_scan_poses', self._getScanPosesCallback)
        self._stopService = self.create_service(Trigger, 'stop', self._stopCallback)
        self._trigStartStopService = self.create_service(Trigger, 'trig_start_stop', self._trigStartStopCallback)
        self._getScanPatterns = self.create_service(GetScanPatterns, 'get_scan_patterns', self._getScanPatternsCallback)
        self._shutdownService = self.create_service(Trigger, 'shutdown', self._shutdownCallback)

        self._rieglVz = RieglVz(self)

        self._scanposName = "0"
        self.projectValid = False

        self._statusUpdater = Updater(self)
        self._statusUpdater.setHardwareID('riegl_vz')
        self._statusUpdater.add("scanner", self._produceScannerDiagnostics)
        self._statusUpdater.add("memory", self._produceMemoryDiagnostics)

        self._gnssFixTimer = self.create_timer(1.0, self._publishGnssFix)

        self.get_logger().info("RIEGL VZ node is started... (host = {}).".format(self.hostname))

    def _produceScannerDiagnostics(self, diag):
        status = self._rieglVz.getScannerStatus()

        err = DiagnosticStatus.OK
        if status.err:
            err = DiagnosticStatus.ERROR

        diag.summary(err, "RIEGL VZ laser scanner is " + status.opstate)
        diag.add('opstate', status.opstate)
        diag.add('active_task', status.activeTask)
        diag.add('progress', str(status.progress))
        diag.add('scan_position', self._scanposName)

        return diag

    def _produceMemoryDiagnostics(self, diag):
        status = self._rieglVz.getMemoryStatus(self.storageMedia)

        memStatus = "ok"
        if status.memUsage >= 90.0:
            memStatus = "almost full"
            err = DiagnosticStatus.WARN
        if status.memUsage >= 99.0:
            memStatus = "full"
            err = DiagnosticStatus.ERROR

        err = DiagnosticStatus.OK
        if status.err:
            err = DiagnosticStatus.ERROR

        diag.summary(err, "RIEGL VZ storage media is " + memStatus)
        diag.add('mem_total_gb', str(status.memTotalGB))
        diag.add('mem_free_gb', str(status.memFreeGB))
        diag.add('mem_usage', str(status.memUsage))

        return diag

    def _publishGnssFix(self):
            self._rieglVz.publishGnssFix()

    def _setResponseStatus(self, response, success, message):
        response.success = success
        response.message = message
        return success, response

    def _setResponseSuccess(self, response):
        return self._setResponseStatus(response, True, "success")[1]

    def _setResponseExecError(self, response):
        self._logger.error("Service request command execution error!")
        return self._setResponseStatus(response, False, "command execution error")[1]

    def _setResponseException(self, response):
        self._logger.error("Service request command exception!")
        return self._setResponseStatus(response, False, "command execution error")[1]

    def _checkExecConditions(self):
        success = True
        message = "success"
        if not self._rieglVz.isScannerAvailable() or self._shutdownReq:
            success = False
            message = "scanner is not available"
            self._logger.info("Scanner is not available.")
        return success, message

    def _setProjectName(self, projectName):
        if projectName == "":
            now = datetime.now()
            self.projectName = now.strftime("%y%m%d_%H%M%S")
        else:
            self.projectName = projectName

    def _createProject(self, projectName):
        self._setProjectName(projectName)
        self.storageMedia = int(self.get_parameter('storage_media').value)
        ok = True
        if not self._rieglVz.createProject(self.projectName, self.storageMedia):
            ok = False
        return ok

    def _loadProject(self, projectName):
        self.storageMedia = int(self.get_parameter('storage_media').value)
        self.scanRegister = bool(self.get_parameter('scan_register').value)
        ok = True
        if projectName == "" or not self._rieglVz.loadProject(self.projectName, self.storageMedia, self.scanRegister):
            ok = False
        else:
            self.projectName = projectName
        return ok

    def setProject(self, projectName):
        ok = True
        if not self._loadProject(self.projectName):
            ok = self._createProject(self.projectName)

        if ok:
            self.projectValid = True
            self.cpsCsvFile = str(self.get_parameter('control_points_csv_file').value)
            self.get_logger().debug("control points CSV file = {}".format(self.cpsCsvFile))
            if len(self.cpsCsvFile) > 0:
                self.cpsCoordSystem = str(self.get_parameter('control_points_coord_system').value)
                self.get_logger().debug("control points coord system = {}".format(self.cpsCsvCoordSystem))
                self._rieglVz.setProjectControlPoints(self.cpsCoordSystem, self.cpsCsvFile)

        return ok

    def _setProjectCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            self.projectName = str(self.get_parameter('project_name').value)
            self.storageMedia = int(self.get_parameter('storage_media').value)
            self.get_logger().debug("project name = {}".format(self.projectName))

            if not self.setProject(self.projectName):
                self._setResponseExecError(response)
                return response

            self._scanposName = self._rieglVz.getCurrentScanpos(self.projectName, self.storageMedia)
        except:
            self._setResponseException(response)

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
        self.scanPattern.measProgram = int(self.get_parameter('meas_program').value)
        self.scanPublish = bool(self.get_parameter('scan_publish').value)
        self.scanPublishFilter = str(self.get_parameter('scan_publish_filter').value)
        self.scanPublishLOD = int(self.get_parameter('scan_publish_lod').value)
        self.scanRegister = bool(self.get_parameter('scan_register').value)
        self.reflSearchSettings = None
        self.reflSearch = bool(self.get_parameter('reflector_search').value)
        reflSearchModels = str(self.get_parameter('reflector_search_models').value)
        reflSearchLimits = self.get_parameter('reflector_search_limits').value
        if self.reflSearch and (len(reflSearchModels) > 0):
            self.reflSearchSettings = {
                "searchMode": 'model',
                "searchModels": [x.strip() for x in reflSearchModels.split(',')],
                "searchMinRange": reflSearchLimits[0],
                "searchMaxRange": reflSearchLimits[1]
            }

        if not self.projectValid:
            self.setProject(self.projectName)

        self._scanposName = self._rieglVz.getNextScanpos(self.projectName, self.storageMedia)

        return self._rieglVz.scan(
            projectName = self.projectName,
            scanposName = self._scanposName,
            storageMedia = self.storageMedia,
            scanPattern = self.scanPattern,
            scanPublishFilter = self.scanPublishFilter,
            scanPublish = self.scanPublish,
            scanPublishLOD = self.scanPublishLOD,
            scanRegister = self.scanRegister,
            reflSearchSettings = self.reflSearchSettings if self.reflSearch else None,
            captureImages = False,
            captureMode = 1,
            imageOverlap = 25)

    def _scanCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            if not self.scan():
                self._setResponseException(response)
                return response

                self._statusUpdater.force_update
        except:
            self._setResponseException(response)

        return response

    def getPointCloud(self, scanpos, pointcloud):
        ok, pointcloud = self._rieglVz.getPointCloud(scanpos, pointcloud)
        return ok, pointcloud

    def _getPointCloudCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            ok, response.pointcloud = self.getPointCloud(request.seq, response.pointcloud)
            if not ok:
                self._setResponseExecError(response)
                return response
        except:
            self._setResponseException(response)

        return response

    def setPosition(self, position):
        return self._rieglVz.setPosition(position)

    def _setPositionCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            self.setPosition(request.position)
        except:
            self._setResponseException(response)

        return response

    def getSopv(self):
        return self._rieglVz.getSopv()

    def _getSopvCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            ok, sopv = self.getSopv()
            if not ok:
                self._setResponseExecError(response)
                return response

            response.pose = sopv.pose
        except:
            self._setResponseException(response)

        return response

    def getAllSopv(self):
        return self._rieglVz.getAllSopv()

    def getVop(self):
        return self._rieglVz.getVop()

    def getPop(self):
        return self._rieglVz.getPop()

    def _getScanPosesCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            response.project = self.projectName

            ok, sopvs = self.getAllSopv()
            if not ok:
                self._setResponseExecError(response)
                return response

            for sopv in sopvs:
                response.scanposes.append(sopv)

            ok, vop = self.getVop()
            if not ok:
                self._setResponseExecError(response)
                return response

            response.vop = vop

            ok, pop = self.getPop()
            if ok:
                response.pop = pop
            else:
                response.pop = PoseStamped()
        except:
            self._setResponseException(response)

        return response

    def _getVopCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            ok, vop = self.getVop()
            if not ok:
                self._setResponseExecError(response)
                return response

            response.pose = vop
        except:
            self._setResponseException(response)

        return response

    def _getPopCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            ok, pop = self.getPop()
            if not ok:
                self._setResponseExecError(response)
                return response

            response.pose = pop
        except:
            self._setResponseException(response)

        return response

    def stop(self):
        self._rieglVz.stop()

    def _stopCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            self.stop()
        except:
            self._setResponseException(response)

        return response

    def trigStartStop(self):
        return self._rieglVz.trigStartStop()

    def _trigStartStopCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            if not self.trigStartStop():
                self._setResponseExecError(response)
                return response
        except:
            self._setResponseException(response)

        return response

    def getScanPatterns(self):
        return self._rieglVz.getScanPatterns()

    def _getScanPatternsCallback(self, request, response):
        try:
            if not self._setResponseStatus(response, *self._checkExecConditions())[0]:
                return response

            ok, patterns = self.getScanPatterns()
            if not ok:
                self._setResponseExecError(response)
                return response

            for pattern in patterns:
                response.patterns.append(pattern)
        except:
            self._setResponseException(response)

        return response

    def shutdown(self):
        self._shutdownReq = True
        self.stop()
        self._rieglVz.shutdown()

    def _shutdownCallback(self, request, response):
        try:
            self.shutdown()
            self._setResponseSuccess(response)
        except:
            self._setResponseException(response)

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
