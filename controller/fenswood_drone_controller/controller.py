import rclpy
from rclpy.node import Node

# import message definitions for receiving status and position
from mavros_msgs.msg import State, WaypointList
from sensor_msgs.msg import NavSatFix, BatteryState
# import message definition for sending setpoint
from geographic_msgs.msg import GeoPoseStamped

from std_msgs.msg import Bool, Empty, Int16

from geometry_msgs.msg import PoseStamped

# import service definitions for changing mode, arming, take-off and generic command
from mavros_msgs.srv import SetMode, CommandBool, CommandTOL, CommandLong


class FenswoodDroneController(Node):

    def __init__(self):
        super().__init__('controller')
        self.last_status = None     # store for last received status message
        self.last_pos = None       # store for last received position message
        self.init_alt = None       # store for global altitude at start
        self.last_alt_rel = None   # store for last altitude relative to start
        self.last_pose = None
        self.current_mode = None
        self.user_command = 'init'
        self.waypoints = []
        self.waypoints_index = 0
        self.in_fly = False
        self.fail_safe = False
        self.current_battery = None
        # create service clients for long command (datastream requests)...
        self.cmd_cli = self.create_client(CommandLong, '/vehicle_1/mavros/cmd/command')
        # ... for mode changes ...
        self.mode_cli = self.create_client(SetMode, '/vehicle_1/mavros/set_mode')
        # ... for arming ...
        self.arm_cli = self.create_client(CommandBool, '/vehicle_1/mavros/cmd/arming')
        # ... and for takeoff
        self.takeoff_cli = self.create_client(CommandTOL, '/vehicle_1/mavros/cmd/takeoff')

        self.land_cli = self.create_client(CommandTOL, '/vehicle_1/mavroos/cmd/land')
        # create publisher for setpoint
        self.target_pub = self.create_publisher(GeoPoseStamped, '/vehicle_1/mavros/setpoint_position/global', 10)
        # and make a placeholder for the last sent target
        self.last_target = GeoPoseStamped()
        # initial state for finite state machine
        self.control_state = 'init'
        # timer for time spent in each state
        self.state_timer = 0

    def start(self):
        # set up two subscribers, one for vehicle state...
        state_sub = self.create_subscription(State, '/vehicle_1/mavros/state', self.state_callback, 10)
        # ...and the other for global position
        pos_sub = self.create_subscription(NavSatFix, '/vehicle_1/mavros/global_position/global', self.position_callback, 10)

        pose_sub = self.create_subscription(PoseStamped, '/vehicle_1/mavros/local_position/pose', self.pose_callback, 10)

        mission_start_sub = self.create_subscription(Empty, '/mission_start', self.start_callback, 10)

        mission_waypoint_sub = self.create_subscription(WaypointList, '/vehicle_1/mavros/cutom_waypoints', self.waypoints_callback, 10)

        battery_sub = self.create_subscription(BatteryState, '/vehicle_1/mavros/battery', self.battery_callback, 10)

        mission_pause_sub = self.create_subscription(Empty, '/mission_pause', self.emergency_stop_callback, 10)

        mode_control_sub = self.create_subscription(Int16, '/vehicle_1/mode_control', self.mode_mannual_callback, 10)

        # create a ROS2 timer to run the control actions
        self.timer = self.create_timer(1.0, self.timer_callback)

    # on receiving status message, save it to global
    def state_callback(self,msg):
        self.last_status = msg
        self.get_logger().debug('Mode: {}.  Armed: {}.  System status: {}'.format(msg.mode,msg.armed,msg.system_status))

    # on receiving positon message, save it to global
    def position_callback(self,msg):
        # determine altitude relative to start
        if self.init_alt:
            self.last_alt_rel = msg.altitude - self.init_alt
        self.last_pos = msg
        self.get_logger().debug('Drone at {}N,{}E altitude {}m'.format(msg.latitude,
                                                                        msg.longitude,
                                                                        self.last_alt_rel))
    
    def pose_callback(self,msg):
        self.last_pose = msg.pose
        self.get_logger().debug('Drone at local position X:{}, Y:{}, Z:{}'.format(msg.pose.position.x,
                                                                                    msg.pose.position.y,
                                                                                    msg.pose.position.z))
       
        self.get_logger().debug('Drone at local orientation X:{}, Y:{}, Z:{}, W:{}'.format(msg.pose.orientation.x,
                                                                                   msg.pose.orientation.y,
                                                                                    msg.pose.orientation.z,
                                                                                    msg.pose.orientation.w))
    def battery_callback(self,msg):
        self.current_battery = (float) (msg.percentage * 100)
        self.get_logger().info('Battery status: {}%'.format(self.current_battery))
        
    def start_callback(self,msg):
        if (self.user_command != 'run'):
            if(self.waypoints == None or len(self.waypoints) == 0):
                self.get_logger().warn('START button pressed, but the waypoint list is empty.')
            elif(self.control_state != 'arming'):
                self.get_logger().warn('Please wait for the finish of initialisation.')
            else:
                self.user_command = 'run'
                self.get_logger().info('START button pressed. The process of controller will be started.')
    
    def waypoints_callback(self,msg):
        self.waypoints = msg.waypoints
        self.get_logger().info('Waypoints set. {} waypoints passed to the drone.'.format(len(self.waypoints)))

    def mode_mannual_callback(self,msg):
        if (self.user_command == 'pause'):
            if(msg.data == 0):
                self.change_mode('MANNUAL')
                self.get_logger().info('Change to Mannual mode')
            elif(msg.data == 1):
                self.change_mode('LOITER')
                self.get_logger().info('Change to Loiter mode')
            elif(msg.data == 2):
                self.change_mode('GUIDED')
                self.get_logger().info('Change to Guided mode')
            elif(msg.data == 3):
                self.change_mode('RTL')
                self.get_logger().info('Change to RTL mode')
            else:
                self.get_logger().warn('Unepxeted changing mode. Please check as following. \n Input 0: Mannual Mode; \n Input 1: Loiter Mode; \n Input 2: Guided Mode; \n Input 3: RTL Mode;')
        else:
            self.get_logger().warn('Notice that: the mode can only be changed under Pause condition')

    
    def emergency_stop_callback(self,msg):
        if self.control_state != 'init' and self.control_state != 'arming' and self.control_state != 'exit':
            self.user_command = 'pause'
            self.change_mode('LOITER')
            self.get_logger().warn('Pause button is pressed and the drone will keep current position.')
        else:
            self.get_logger().warn('Pause button is not avialable under current state')

    def request_data_stream(self,msg_id,msg_interval):
        cmd_req = CommandLong.Request()
        cmd_req.command = 511
        cmd_req.param1 = float(msg_id)
        cmd_req.param2 = float(msg_interval)
        future = self.cmd_cli.call_async(cmd_req)
        self.get_logger().info('Requested msg {} every {} us'.format(msg_id,msg_interval))

    def change_mode(self,new_mode):
        if(self.current_mode != new_mode):
            mode_req = SetMode.Request()
            mode_req.custom_mode = new_mode
            self.mode = new_mode
            future = self.mode_cli.call_async(mode_req)
            self.get_logger().info('Request sent for {} mode.'.format(new_mode))
        else:
            self.get_logger().info('Tried changing mode to {} mode, but the drone has been in this mode already'.format(new_mode))

    def arm_request(self):
        arm_req = CommandBool.Request()
        arm_req.value = True
        future = self.arm_cli.call_async(arm_req)
        self.get_logger().info('Arm request sent')
    
    def land(self):
        land_req = CommandTOL.Request()
        future = self.land_cli.call_async(land_req)
        self.get_logger().info('Land request sent')

    def takeoff(self,target_alt):
        takeoff_req = CommandTOL.Request()
        takeoff_req.altitude = target_alt
        future = self.takeoff_cli.call_async(takeoff_req)
        self.get_logger().info('Requested takeoff to {}m'.format(target_alt))

    def flyto(self,lat,lon,alt):
        self.last_target.pose.position.latitude = lat
        self.last_target.pose.position.longitude = lon
        self.last_target.pose.position.altitude = alt
        self.target_pub.publish(self.last_target)
        self.get_logger().info('Sent drone to {}N, {}E, altitude {}m'.format(lat,lon,alt)) 

    def state_transition(self):
        if self.user_command == 'init':
            if self.control_state == 'init':
                if self.last_status:
                    if self.last_status.system_status==3:
                        self.get_logger().info('Drone initialized')
                        # send command to request regular position updates
                        self.request_data_stream(33, 1000000)

                        self.request_data_stream(32, 1000000)

                        self.request_data_stream(147, 1000000)
                        # change mode to GUIDED
                        self.change_mode("GUIDED")
                        # move on to arming
                        return('arming')
                    else:
                        return('init')
                else:
                    return('init')
            elif self.control_state == 'arming':
                self.get_logger().info('Waiting for user commands. Press the START button to start the mission.')
                self.state_timer = 0
                return('arming')
            else:
                return('init')
        elif self.user_command == 'run':
            if self.control_state == 'arming':
                if self.last_status.armed:
                    self.get_logger().info('Arming successful')
                    # armed - grab init alt for relative working
                    if self.last_pos:
                        self.last_alt_rel = 0.0
                        self.init_alt = self.last_pos.altitude
                    # send takeoff command
                    self.takeoff(20.0)
                    return('climbing')
                elif self.state_timer > 60:
                    # timeout
                    self.get_logger().error('Failed to arm')
                    return('exit')
                else:
                    self.arm_request()
                    return('arming')
            elif self.control_state == 'climbing':
                if self.last_alt_rel > 19.0:
                    self.get_logger().info('Close enough to flight altitude')
                    # move drone by sending setpoint message
                    # self.flyto(51.423, -2.671, self.init_alt - 30.0) # unexplained correction factor on altitude
                    return('on_way')
                elif self.state_timer > 60:
                    # timeout
                    self.get_logger().error('Failed to reach altitude')
                    return('RTL')
                else:
                    self.get_logger().info('Climbing, altitude {}m'.format(self.last_alt_rel))
                    return('climbing')
            elif self.control_state == 'on_way':
                if(self.in_fly == False):
                   lat = self.waypoints[self.waypoints_index].x_lat
                   long = self.waypoints[self.waypoints_index].y_long
                   alt = self.waypoints[self.waypoints_index].z_alt - 50
                   self.flyto(lat, long, alt)
                   self.in_fly = True
                d_lon = self.last_pos.longitude - self.last_target.pose.position.longitude
                d_lat = self.last_pos.latitude - self.last_target.pose.position.latitude
                if (abs(d_lon) < 0.0001) & (abs(d_lat) < 0.0001):
                    self.get_logger().info('Close enough to target delta={},{}'.format(d_lat,d_lon))
                    if(self.waypoints_index < len(self.waypoints) - 1):
                        self.waypoints_index += 1
                        self.in_fly = False
                        return ('on_way')
                    else:
                        return('RTL')
                elif self.state_timer > 300:
                    # timeout
                    self.get_logger().error('Failed to reach target')
                    return('RTL')
                else:
                    self.get_logger().info('Target error {},{}'.format(d_lat,d_lon))
                    return('on_way')
                
            elif self.control_state == 'landing':
                # return home and land
                # if(self.current_mode != 'LOITER'):
                #    self.change_mode('LOITER')
                self.land()
                return('landing')
                #self.change_mode("Landing")
                #return('landing')

            elif self.control_state == 'RTL':
                if(self.current_mode != 'RTL'):
                    self.change_mode("RTL")
                return 'RTL'

            elif self.control_state == 'exit':
                # nothing else to do
                return('exit')
        elif self.user_command == 'pause':
            if self.control_state == 'climbing':
                return('climbing')
            elif self.control_state == 'on_way':
                return('on_way')
                
            elif self.control_state == 'landing':
                return('landing')
                #self.change_mode("Landing")
                #return('landing')

            elif self.control_state == 'RTL':
                return 'RTL'

            elif self.control_state == 'exit':
                # nothing else to do
                return('exit')
        else:
            self.get_logger().error('Unepected user command here. Landing for emergency conditions.')
            return 'landing'

    def timer_callback(self):
        new_state = self.state_transition()
        if new_state == self.control_state:
            self.state_timer = self.state_timer + 1
        else:
            self.state_timer = 0
        self.control_state = new_state
        self.get_logger().info('Controller state: {} for {} steps'.format(self.control_state, self.state_timer))
                    

def main(args=None):
    
    rclpy.init(args=args)

    controller_node = FenswoodDroneController()
    controller_node.start()
    rclpy.spin(controller_node)


if __name__ == '__main__':
    main()