#!/usr/bin/env python

'''
Created on 12 Mar 2013

@author: nathan
'''
from collections import namedtuple

from threading import Thread
import math
import rospy
import time


from controller import Robot
try:
    import roslib
    import os
    path = os.path.dirname(os.path.realpath(__file__))
    roslib.load_manifest('rosController')
except:
    import logging
    logger = logging.getLogger()
    if logger.handlers:
        logging.getLogger().error(
            "Unable to load roslib, fatal error", exc_info=True)
    else:
        import sys
        import traceback
        print >> sys.stderr, "Unable to load roslib, fatal error"
        print >> sys.stderr, traceback.format_exc()
    exit(1)
else:
    import sf_controller_msgs.msg
    import actionlib
    from geometry_msgs.msg import PoseStamped, Twist
    from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
    from nav_msgs.msg import Odometry
    # from p2os_msgs.msg import SonarArray
    from rosgraph_msgs.msg import Clock
    from sensor_msgs.msg import LaserScan, JointState
    from tf import TransformBroadcaster
    from tf.transformations import quaternion_from_euler


_states = {
    0: 'PENDING',
    'PENDING': 0,
    1: 'ACTIVE',
    'ACTIVE': 1,
    2: 'PREEMPTED',
    'PREEMPTED': 2,
    3: 'SUCCEEDED',
    'SUCCEEDED': 3,
    4: 'ABORTED',
    'ABORTED': 4,
    5: 'REJECTED',
    'REJECTED': 5,
    6: 'PREEMPTING',
    'PREEMPTING': 6,
    7: 'RECALLING',
    'RECALLING': 7,
    8: 'RECALLED',
    'RECALLED': 8,
    9: 'LOST',
    'LOST': 9,
}


class ClockSync(Thread):

    def __init__(self, robot):
        super(ClockSync, self).__init__()
        self.getTime = robot.getTime
        self._stop = False

    def run(self):
        try:
            # ROS Version >= Hydro
            clockPublisher = rospy.Publisher("/clock", Clock, queue_size=2)
        except:
            clockPublisher = rospy.Publisher("/clock", Clock)

        self._stop = False
        while not rospy.is_shutdown() and not self._stop:
            rosTime = rospy.Time(self.getTime())
            clock = Clock(clock=rosTime)
            clockPublisher.publish(clock)
            time.sleep(0)

    def stop(self):
        self._stop = True


class Sunflower(Robot):

    _actionHandles = {}
    Location = namedtuple('Location', ['x', 'y', 'theta', 'timestamp'])
    DistanceScan = namedtuple('DistanceScan', [
                              'min_angle',
                              'max_angle',
                              'min_range',
                              'max_range',
                              'scan_time',
                              'ranges'])

    # TODO: These should be in a config file
    # Speed limits from navigation files
    _translationSpeed = [-0.2, 0.4]
    _rotationSpeed = [-0.8, 0.8]

    def __init__(self, name):
        super(Sunflower, self).__init__()
        self._time_step = int(self.getBasicTimeStep())
        self._action_name = name
        self._as = actionlib.SimpleActionServer(
            self._action_name,
            sf_controller_msgs.msg.SunflowerAction,
            execute_cb=self.execute_cb,
            auto_start=False)
        self._as.start()
        try:
            # ROS Version >= Hydro
            self._cmdVel = rospy.Subscriber(
                'cmd_vel',
                Twist,
                callback=self.cmdvel_cb,
                queue_size=2)
        except:
            self._cmdVel = rospy.Subscriber(
                'cmd_vel',
                Twist,
                callback=self.cmdvel_cb)

        rospy.loginfo(
            "Started Sunflower Controller ActionServer on topic %s",
            self._action_name)
        self._feedback = sf_controller_msgs.msg.SunflowerFeedback()
        self._result = sf_controller_msgs.msg.SunflowerResult()
        self._location = None
        self._lastLocation = None
        self._rosTime = None
        self._servos = {}
        self._sensors = {}
        self._sensorValues = {}
        self._leds = {}
        self.initialise()

    def _updateLocation(self):
        if self._sensors.get('gps', None) and self._sensors.get('compass', None):
            lX, lY, lZ = self._sensors['gps'].getValues()
            wX, _, wZ = self._sensors['compass'].getValues()

            # http://www.cyberbotics.com/reference/section3.13.php
            bearing = math.atan2(wX, wZ) - (math.pi / 2)
            x, y, _, rotation = self.webotsToRos(lX, lY, lZ, bearing)

            self._lastLocation = self._location
            self._location = Sunflower.Location(x, y, rotation, self.getTime())

    def _updateSonar(self):
        if self._sensors.get('sonar', None):
            self._sensorValues['sonar'] = map(
                lambda x: x.getValue(), self._sensors['sonar'])

    def _updateLaser(self):
        if self._sensors.get('frontLaser', None):
            fov = self._sensors['frontLaser'].getFov()
            ranges = self._sensors['frontLaser'].getRangeImage()
            ranges.reverse()
            maxRange = self._sensors['frontLaser'].getMaxRange()
            sampleRate = self._sensors['frontLaser'].getSamplingPeriod() / 1000

            self._sensorValues['frontLaser'] = Sunflower.DistanceScan(
                fov / -2,
                fov / 2,
                0,
                maxRange,
                sampleRate,
                ranges
            )

    def _publishOdomTransform(self, odomPublisher):
        if self._location:
            odomPublisher.sendTransform(
                (self._location.x, self._location.y, 0),
                quaternion_from_euler(0, 0, self._location.theta),
                self._rosTime,
                'base_link',
                'odom')

    def _publishLocationTransform(self, locationPublisher):
        if self._location:
            locationPublisher.sendTransform(
                (self._location.x, self._location.y, 0),
                quaternion_from_euler(0, 0, self._location.theta),
                self._rosTime,
                'odom',
                'map',)

    def _publishLaserTransform(self, laserPublisher):
        if self._location:
            laserPublisher.sendTransform(
                (0.0, 0.0, 0.0),
                quaternion_from_euler(0, 0, 0),
                self._rosTime,
                'scan_front',
                'base_laser_front_link')

    def _publishOdom(self, odomPublisher):
        if self._location:
            msg = Odometry()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = 'odom'
            msg.child_frame_id = "base_link"

            msg.pose.pose.position.x = self._location.x
            msg.pose.pose.position.y = self._location.y
            msg.pose.pose.position.z = 0

            orientation = quaternion_from_euler(
                0, 0, self._location.theta)
            msg.pose.pose.orientation.x = orientation[0]
            msg.pose.pose.orientation.y = orientation[1]
            msg.pose.pose.orientation.z = orientation[2]
            msg.pose.pose.orientation.w = orientation[3]

            odomPublisher.publish(msg)
        else:
            rospy.logerr("Skipped updating odom! Last: %s, Cur: %s" % 
                         (self._location, self._lastLocation))

    def _publishPose(self, posePublisher):
        if self._location:
            msg = Odometry()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = 'odom'
            msg.child_frame_id = 'base_link'

            msg.pose.pose.position.x = self._location.x
            msg.pose.pose.position.y = self._location.y
            msg.pose.pose.position.z = 0

            orientation = quaternion_from_euler(
                0, 0, self._location.theta)
            msg.pose.pose.orientation.x = orientation[0]
            msg.pose.pose.orientation.y = orientation[1]
            msg.pose.pose.orientation.z = orientation[2]
            msg.pose.pose.orientation.w = orientation[3]

            if self._lastLocation:
                dt = (
                    self._location.timestamp - self._lastLocation.timestamp) / 1000
                msg.twist.twist.linear.x = (
                    self._location.x - self._lastLocation.x) / dt
                msg.twist.twist.linear.y = (
                    self._location.y - self._lastLocation.y) / dt
                msg.twist.twist.angular.x = (
                    self._location.theta - self._lastLocation.theta) / dt

            posePublisher.publish(msg)

    def _publishSonar(self, sonarPublisher):
        if self._sensorValues.get('sonar', None):
            # msg = SonarArray()
            # msg.header.stamp = self._rosTime

            # msg.ranges = self._sensorValues['sonar']
            # msg.ranges_count = len(self._sensorValues['sonar'])
            # sonarPublisher.publish(msg)
            pass

    def _publishLaser(self, laserPublisher):
        if self._sensorValues.get('frontLaser', None):
            laser_frequency = 40
            msg = LaserScan()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = 'scan_front'

            msg.ranges = self._sensorValues['frontLaser'].ranges
            msg.angle_min = self._sensorValues['frontLaser'].min_angle
            msg.angle_max = self._sensorValues['frontLaser'].max_angle
            msg.angle_increment = abs(
                msg.angle_max - msg.angle_min) / len(msg.ranges)
            msg.range_min = self._sensorValues['frontLaser'].min_range
            msg.range_max = self._sensorValues['frontLaser'].max_range
            msg.scan_time = self._time_step
            msg.time_increment = (msg.scan_time / laser_frequency / 
                                  len(self._sensorValues['frontLaser'].ranges))
            laserPublisher.publish(msg)

    def _publishJoints(self, jointPublisher):
        # header:
        #   seq: 375
        #   stamp:
        #     secs: 1423791124
        #     nsecs: 372004985
        #   frame_id: ''
        # name: ['head_pan_joint', 'neck_lower_joint', 'swivel_hubcap_joint', 'base_swivel_joint', 'head_tilt_joint', 'neck_upper_joint']
        # position: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        # velocity: []
        # effort: []
        if self._servos and jointPublisher:
            msg = JointState()
            msg.header.stamp = self._rosTime
            names = []
            positions = []
            for (name, servo) in self._servos.iteritems():
                names.append("%s_joint" % name)
                # 'or 0.0' to prevent null from being published
                positions.append(servo.getPosition() or 0.0)

            msg.name = names
            msg.position = positions
            jointPublisher.publish(msg)

    def run(self):
        try:
            # ROS Version >= Hydro
            # sonarPublisher = rospy.Publisher("/sonar", SonarArray, queue_size=2)
            odomPublisher = rospy.Publisher("/odom", Odometry, queue_size=2)
            posePublisher = rospy.Publisher("/pose", Odometry, queue_size=2)
            laserPublisher = rospy.Publisher(
                "/scan_front", LaserScan, queue_size=2)
            # clockPublisher = rospy.Publisher("/clock", Clock, queue_size=2)
            jointPublisher = rospy.Publisher(
                "/joint_states", JointState, queue_size=2)
        except:
            # sonarPublisher = rospy.Publisher("/sonar", SonarArray)
            odomPublisher = rospy.Publisher("/odom", Odometry)
            posePublisher = rospy.Publisher("/pose", Odometry)
            laserPublisher = rospy.Publisher("/scan_front", LaserScan)
            # clockPublisher = rospy.Publisher("/clock", Clock)
            jointPublisher = rospy.Publisher("/joint_states", JointState)
        odomTransform = TransformBroadcaster()
        locationTransform = TransformBroadcaster()
        laserTransform = TransformBroadcaster()

        # Probably something wrong elsewhere, but we seem to need to publish
        # map->odom transform once to get sf_navigation to load
        self._publishLocationTransform(locationTransform)
        cs = ClockSync(sf)
        cs.start()

        while not rospy.is_shutdown() and self.step(self._time_step) != -1:
            # self._rosTime = rospy.Time(self.getTime())
            self._rosTime = rospy.Time.now()

            self._updateLocation()
            self._updateSonar()
            self._updateLaser()
            # Published by ClockSync
            # self._publishClock(clockPublisher)
            self._publishPose(posePublisher)
            self._publishOdom(odomPublisher)
            self._publishOdomTransform(odomTransform)
            # Published by robot_joint_publisher
            self._publishLaserTransform(laserTransform)
            # Published by sf_navigation
            # self._publishLocationTransform(locationTransform)
            # self._publishSonar(sonarPublisher)
            self._publishLaser(laserPublisher)
            self._publishJoints(jointPublisher)

            # It appears that we have to call sleep for ROS to process messages
            time.sleep(0.0001)

        cs.stop()
        cs.join()

    def webotsToRos(self, x, y, z, theta):
        rX = -z
        rY = -x
        rZ = y
        theta = -1 * theta
        return (rX, rY, rZ, theta)

    def initialise(self):
        numLeds = 3
        # numSonarSensors = 16

        self._leftWheel = self.getMotor("left_wheel")
        self._leftWheel.setPosition(float('+inf'))
        self._leftWheel.setVelocity(0)

        self._rightWheel = self.getMotor("right_wheel")
        self._rightWheel.setPosition(float('+inf'))
        self._rightWheel.setVelocity(0)

        self._servos = {
            "tray": self.getMotor("tray"),
            "neck_lower": self.getMotor("neck_lower"),
            "neck_upper": self.getMotor("neck_upper"),
            "head_tilt": self.getMotor("head_tilt"),
            "head_pan": self.getMotor("head_pan"),
        }

        for servo in self._servos.values():
            servo.enablePosition(self._time_step)

        self._sensors['frontLaser'] = self.getCamera("front_laser")
        self._sensors['frontLaser'].enable(self._time_step)

        self._sensors['gps'] = self.getGPS("gps")
        self._sensors['gps'].enable(self._time_step)

        self._sensors['compass'] = self.getCompass("compass")
        self._sensors['compass'].enable(self._time_step)

        self._sensors['camera'] = self.getCamera("head_camera")
        if self._sensors['camera']:
            self._sensors['camera'].enable(self._time_step)

        self._leds['body'] = self.getLED("light")

        self._leds['base'] = []
        for i in range(0, numLeds):
            led = self.getLED("red_led%s" % (i + 1))
            led.set(0)
            self._leds['base'].append(led)

        # self._sensors['sonar'] = []
        # for i in range(0, numSonarSensors):
        #    sensor = self.getDistanceSensor("so%s" % i)
        #    sensor.enable(self._time_step)
        #    self._sensors['sonar'].append(sensor)

    def park(self):
        pass

    def execute_cb(self, goal):
        rospy.loginfo("Got goal: %s" % goal)
        if goal.component == 'light':
            result = self.setlight(goal.jointPositions)
        elif goal.action == 'move':
            result = self.move(goal)
        elif goal.action == 'init':
            result = self.init(goal.component)
        elif goal.action == 'stop':
            self.stop(goal.component)
            result = True
        elif goal.action == 'park':
            self.park()
            result = True
        else:
            rospy.logwarn("Unknown action %s", goal.action)
            self._result.result = -1
            self._as.set_aborted(self._result)

        if result:
            self._result.result = 0
            self._as.set_succeeded(self._result)
        else:
            self._result.result = -1
            self._as.set_aborted(self._result)

    def init(self, name):
        return True

    def stop(self, name):
        rospy.loginfo("%s: Stopping %s", self._action_name, name)
        if name == 'base':
            client = actionlib.SimpleActionClient('/move_base', MoveBaseAction)
            client.wait_for_server()
            client.cancel_all_goals()
        else:
            pass

    def setlight(self, color):
        # Sunflower hardware only supports on/off states for RGB array
        # Webots selects the color as an array index of available colors
        # 3-bit color array is arranged in ascending binary order
        try:
            r = 0x4 if color[0] else 0
            g = 0x2 if color[1] else 0
            b = 0x1 if color[2] else 0
            # Webots color array is 1-indexed
            colorIndex = r + g + b + 1
            if self._leds['body']:
                self._leds['body'].set(colorIndex)
                return True
            else:
                rospy.logerr("Unable to set color.  Body LED not found.")
                return False
        except Exception:
            rospy.logerr("Error setting color to: %s" % (color), exc_info=True)
            return False

    def move(self, goal):
        joints = goal.jointPositions

        if(goal.namedPosition != '' and goal.namedPosition is not None):
            param = '/sf_controller/' + \
                goal.component + '/' + goal.namedPosition
            if(rospy.has_param(param)):
                joints = rospy.get_param(param)[0]

        rospy.loginfo("%s: Setting %s to %s",
                      self._action_name,
                      goal.component,
                      goal.namedPosition or joints)

        try:
            if goal.component == 'base':
                result = self.navigate(goal, joints)
            elif goal.component == 'base_direct':
                result = self.moveBase(goal, joints)
            else:
                result = self.moveJoints(goal, joints)

            rospy.logdebug("%s: '%s to %s' Result:%s",
                           self._action_name,
                           goal.component,
                           goal.namedPosition or joints,
                           result)
        except Exception as e:
            rospy.logerr("Error occurred: %s" % e)
            return False

        return result == _states['SUCCEEDED']

    def moveBase(self, goal, positions):
        maxTransNeg, maxTransPos = self._translationSpeed
        maxRotNeg, maxRotPos = self._rotationSpeed

        LINEAR_RATE = math.pi / 2  # [rad/s]
        # WHEEL_DIAMETER = 0.195  # [m] From the manual
        WHEEL_RADIUS = 0.0975
        # BASE_SIZE = 0.3810  # [m] From the manual
        AXEL_LENGTH = 0.33  # [m] From WeBots Definition
        WHEEL_ROTATION = AXEL_LENGTH / (2 * WHEEL_RADIUS)

        rotation = round(positions[0], 4)
        linear = round(positions[1], 4)

        if not isinstance(rotation, (int, float)):
            rospy.logerr("Non-numeric rotation in list, aborting moveBase")
            return _states['ABORTED']
        elif not isinstance(linear, (int, float)):
            rospy.logerr("Non-numeric translation in list, aborting moveBase")
            return _states['ABORTED']
        if linear > maxTransPos:
            rospy.logerr(
                "Maximal relative translation step exceeded(max: %sm, requested: %sm), "
                "aborting moveBase" % (maxTransPos, linear))
            return _states['ABORTED']
        if linear < maxTransNeg:
            rospy.logerr(
                "Minimal relative translation step exceeded(min: %sm, requested: %sm), "
                "aborting moveBase" % (maxTransNeg, linear))
            return _states['ABORTED']
        if rotation > maxRotPos:
            rospy.logerr(
                "Maximal relative rotation step exceeded(max: %srad, requested: %sm), "
                "aborting moveBase" % (maxRotPos, rotation))
            return _states['ABORTED']
        if rotation < maxRotNeg:
            rospy.logerr(
                "Maximal relative rotation step exceeded(max: %srad, requested: %sm), "
                "aborting moveBase" % (maxRotNeg, rotation))
            return _states['ABORTED']

        rotRads = rotation * WHEEL_ROTATION
        linearRads = linear / WHEEL_RADIUS

        leftDuration = (rotRads + linearRads) / LINEAR_RATE
        rightDuration = ((-1 * rotRads) + linearRads) / LINEAR_RATE

        leftRate = LINEAR_RATE
        rightRate = LINEAR_RATE
        if leftDuration < 0:
            leftRate = -1 * leftRate
            leftDuration = abs(leftDuration)
        if rightDuration < 0:
            rightRate = -1 * rightRate
            rightDuration = abs(rightDuration)
        if leftDuration != rightDuration:
            if leftDuration < rightDuration:
                leftRate = leftRate * (leftDuration / rightDuration)
            else:
                rightRate = rightRate * (rightDuration / leftDuration)

        rospy.loginfo("Setting rates: L=%s, R=%s" % (leftRate, rightRate))
        duration = max(leftDuration, rightDuration)
        start_time = self.getTime()
        end_time = start_time + duration
        while not rospy.is_shutdown():
            if self._as.is_preempt_requested():
                rospy.loginfo('%s: Preempted' % self._action_name)
                self._rightWheel.setVelocity(0)
                self._leftWheel.setVelocity(0)
                # self._as.set_preempted()
                return _states['PREEMPTED']

            self._rightWheel.setVelocity(rightRate)
            self._leftWheel.setVelocity(leftRate)

            if self.getTime() >= end_time:
                break

        self._rightWheel.setVelocity(0)
        self._leftWheel.setVelocity(0)

        return _states['SUCCEEDED']

    def cmdvel_cb(self, msg):
        WHEEL_RADIUS = 0.0975
        
        #Get in-range linear and angular values
        linear = min(max(msg.linear.x, self._translationSpeed[0]), self._translationSpeed[1])
        angular = min(max(msg.angular.z, self._rotationSpeed[0]), self._rotationSpeed[1])
        
        linearRads = linear / WHEEL_RADIUS
        
        #Get in-range p3DX hard limits
        right = max(min(linearRads + angular, 5.24), -5.24)
        left = max(min(linearRads - angular, 5.24), -5.24)
        
        self._rightWheel.setVelocity(right)
        self._leftWheel.setVelocity(left)

    def navigate(self, goal, positions):
        pose = PoseStamped()
        pose.header.stamp = self._rosTime
        pose.header.frame_id = "/map"
        pose.pose.position.x = positions[0]
        pose.pose.position.y = positions[1]
        pose.pose.position.z = 0.0
        q = quaternion_from_euler(0, 0, positions[2])
        pose.pose.orientation.x = q[0]
        pose.pose.orientation.y = q[1]
        pose.pose.orientation.z = q[2]
        pose.pose.orientation.w = q[3]

        client = actionlib.SimpleActionClient('/move_base', MoveBaseAction)
        client_goal = MoveBaseGoal()
        client_goal.target_pose = pose

        client.wait_for_server()
        rospy.loginfo("%s: Navigating to (%s, %s, %s)",
                      self._action_name,
                      positions[0],
                      positions[1],
                      positions[2])

        handle = _ActionHandle(client)
        client.send_goal(client_goal)
        handle.wait()
        return handle.result

    def moveJoints(self, goal, positions):
        try:
            joint_names = rospy.get_param(
                '/sf_controller/%s/joint_names' % 
                goal.component)
        except KeyError:
            # assume component is a named joint
            joint_names = [goal.component, ]
        for i in range(0, len(joint_names)):
            servoName = joint_names[i]
            if servoName not in self._servos:
                rospy.logerr('Undefined joint %s', servoName)
                return _states['ABORTED']
            self._servos[servoName].set(positions[i])

        return _states['SUCCEEDED']


class _ActionHandle(object):
    # ------------------- action_handle section ------------------- #
    # Action handle class.
    #
    # The action handle is used to implement asynchronous behaviour within the
    # script.

    def __init__(self, simpleActionClient):
        # Initialises the action handle.
        self._client = simpleActionClient
        self._waiting = False
        self._result = None

    @property
    def result(self):
        if self._waiting:
            return None

        return self._result

    def wait(self, duration=None):
        t = self.waitAsync(duration)
        t.join()

    def waitAsync(self, duration=None):
        thread = Thread(target=self._wait_for_finished, args=(duration,))
        thread.setDaemon(True)
        thread.start()
        return thread

    def _wait_for_finished(self, duration):
        self._waiting = True
        if duration is None:
            self._client.wait_for_result()
        else:
            self._client.wait_for_result(rospy.Duration(duration))

        self._result = self._client.get_state()
        self._waiting = False

    def cancel(self):
        self._client.cancel_all_goals()


if __name__ == '__main__':
    rospy.init_node('sf_controller')
    sf = Sunflower(rospy.get_name())
    sf.run()