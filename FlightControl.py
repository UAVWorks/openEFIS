# Copyright (C) 2015-2018  Garrett Herschleb
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
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

import logging

import PID
import Common.FileConfig as FileConfig
import Common.util as util

logger=logging.getLogger(__name__)

SUBMODE_COURSE="course"
SUBMODE_TURN="turn"
SUBMODE_STRAIGHT="straight"
SUBMODE_SWOOP_DOWN="swoop_down"
SUBMODE_SWOOP_UP="swoop_up"

class FlightControl(FileConfig.FileConfig):
    def __init__(self, att_cont, throttle_control, sensors, callback):
        # Dynamic Input parameters
        self.DesiredCourse = [(0,0), (0,0)]
        self.DesiredAltitude = 0
        self.DesiredAirSpeed = 0
        self.DesiredTrueHeading = 0
        self._desired_climb_rate = 0
        self.MinClimbAirSpeed = 20.0
        self.JournalPitch = False
        self.JournalThrottle = False
        self.JournalFileName = ''

        # Current State properties
        self.CurrentAltitude = sensors.Altitude()
        self.CurrentAirSpeed = sensors.AirSpeed()
        self.CurrentClimbRate = sensors.ClimbRate()
        self.CurrentPosition = sensors.Position()
        self._journal_file = None
        self._journal_flush_count = 0
        self._in_pid_optimization = ''
        self._pid_optimization_goal = 0
        self._pid_optimization_scoring = None

        # Computed private operating parameters
        self._desired_heading = 0
        self._desired_roll = 0
        self._desired_pitch = 0
        self._last_pitch = 0

        self._flight_mode = SUBMODE_COURSE
        self._callback = callback
        self._force_turn_direction = 0
        self._swoop_low_alt = sensors.Altitude()

        # Configuration properties
        self.AltitudeAchievementMinutes = 1.2
        self.AirSpeedAchievementMinutes = 0.5
        self.ClimbRateAchievementSeconds = 10.0
        self.ClimbRateLimits = (-1000.0, 1000.0)        # feet / minute
        self.PitchPIDLimits = [(0,20.0), (45,0)]  # (roll, max degrees)

        self.PitchPIDSampleTime = 1000
        self.ThrottlePIDSampleTime = 1000

        self.ClimbPitchPIDTuningParams = None
        self.AirspeedPitchPIDTuningParams = None
        self.ThrottlePIDTuningParams = None
        self.RollCurve = [(0.0, 0.0), (5.0, 5.0), (10.0, 20.0), (20.0, 30.0)]

        self.InterceptMultiplier = 20
        self.MaxRoll = 30.0

        self.MaxPitchChangePerSample = 1.0
        self.TurnRate = 180.0         # 180 degrees per minute at max roll
        self.ClimbRateCurve = None

        self.ClimbPitchCurve = None
        self.SwoopPitch = 20.0
        self.SwoopMaxAirSpeed = 200.0
        self.SwoopAltitudeReversal = 200

        self.EngineOutPitchCurve = None

        self.DescentCurve = None
        self.CourseProjectionSeconds = 3.0

        # Will Use one of the following 2 PIDS to request pitch angle:
        # Input: Current Climb Rate; SetPoint: Desired Climb Rate; Output: Desired Pitch Attitude
        self._climbPitchPID = None
        # Input: Current Airspeed; SetPoint: Desired Climb Airspeed; Output: Desired Pitch Attitude
        self._airspeedPitchPID = None
        self._using_airspeed_pitch = False

        # Operational properties
        # Input: Current Airspeed; SetPoint: DesiredAirspeed; Output: throttle setting
        self._throttlePID = None
        self._attitude_control = att_cont
        self._sensors = sensors
        self._throttle_control = throttle_control
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"
        self._rel_lng = 0
        self._descent_starting_altitude = 0 # Used for controlled_descent mode
        self._descent_distance = 0          # Used for controlled_descent mode
        self._nominal_descent_rate = 0      # Used for controlled_descent mode
        self._airspeed_achievement_minutes = 0.0
        self._notify_when_nominal = False

        FileConfig.FileConfig.__init__(self)

    def initialize(self, filelines):
        self.InitializeFromFileLines(filelines)
        ms = util.millis(self._sensors.Time())
        self._last_update_time = ms


        kp,ki,kd = self.ClimbPitchPIDTuningParams
        self._climbPitchPID = PID.PID(0, kp, ki, kd, PID.DIRECT, ms)
        if isinstance(self.PitchPIDLimits,tuple):
            mn,mx = self.PitchPIDLimits
        else:
            _,mx = self.PitchPIDLimits[0]
            mn = -mx
        self._climbPitchPID.SetOutputLimits (mn, mx)

        kp,ki,kd = self.AirspeedPitchPIDTuningParams
        self._airspeedPitchPID = PID.PID(0, kp, ki, kd, PID.DIRECT, ms)
        self._airspeedPitchPID.SetOutputLimits (mn, mx)

        kp,ki,kd = self.ThrottlePIDTuningParams
        self._throttlePID = PID.PID(0, kp, ki, kd, PID.DIRECT, ms)

        self._climbPitchPID.SetSampleTime (self.PitchPIDSampleTime)
        self._airspeedPitchPID.SetSampleTime (self.PitchPIDSampleTime)
        self._throttlePID.SetSampleTime (self.ThrottlePIDSampleTime)
        self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes

    # Notifies that the take off roll has accompished enough speed that flight controls
    # have responsibility and authority. Activates PIDs.
    def Start(self, last_desired_pitch):
        logger.debug ("Flight Control Starting")
        mn,mx = self._throttle_control.GetLimits()
        self._throttle_range = (mn,mx)
        self._throttlePID.SetOutputLimits (mn, mx)

        self.CurrentAirSpeed = self._sensors.AirSpeed()
        self._throttlePID.SetSetPoint (self.DesiredAirSpeed)
        self._throttlePID.SetMode (PID.AUTOMATIC, self.CurrentAirSpeed, self._throttle_control.GetCurrent())
        self.CurrentAltitude = self._sensors.Altitude()
        self._desired_climb_rate = self.get_climb_rate()
        self.CurrentClimbRate = self._sensors.ClimbRate()

        self._desired_pitch = last_desired_pitch
        self._last_pitch = self._sensors.Pitch()
        self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes

        self._attitude_control.StartFlight()

        if self.JournalFileName and (not self._journal_file):
            self._journal_file = open(self.JournalFileName, 'w+')
            if self._journal_file:
                self._journal_file.write("Time")
                if self.JournalPitch:
                    self._journal_file.write(",PitchGoal,PitchInput,DesiredPitch")
                if self.JournalThrottle:
                    self._journal_file.write(",ThrottleSet,ThrottleCurrent,Throttle")
                self._journal_file.write("\n")

    def SetPitch(self, ms):
        if self._pitch_mode == None:
            # Figure out if we're using air speed or climb rate to select pitch:
            if (self._throttle_control.GetCurrent() == self._throttle_range[1] and
                    (self.CurrentAirSpeed <= self.MinClimbAirSpeed  or
                     (self._using_airspeed_pitch and
                         self.CurrentAirSpeed <= self.MinClimbAirSpeed * 1.1)) and
                     self.DesiredAltitude > self.CurrentAltitude):
                # Change to Use airspeed to control pitch
                asret = self.UpdateAirspeedPitch(ms)
                crret = self.UpdateClimbratePitch(ms)
                if asret < crret:
                    self._using_airspeed_pitch = True
                    logger.log (3, "Pitch chosen to preserve airspeed (%g) instead of climb rate (%g)",
                        asret, crret)
                    ret = asret
                else:
                    self._using_airspeed_pitch = False
                    logger.log (3, "Pitch chosen to preserve climb rate (%g) instead of air speed (%g)",
                        crret, asret)
                    ret = crret
            else:
                self._airspeedPitchPID.SetMode (PID.MANUAL, self.CurrentAirSpeed, -self._desired_pitch)
                ret = self.UpdateClimbratePitch(ms)
                self._using_airspeed_pitch = False
        else:       # Pitch mode for controlled descent
            if self._climbPitchPID.GetMode() != PID.AUTOMATIC:
                self._climbPitchPID.SetMode (PID.AUTOMATIC,
                                            self.CurrentClimbRate, self._desired_pitch)
            # Figure out how high or low are we
            remaining_course = [self.CurrentPosition, self.DesiredCourse[1]]
            _,remaining_distance,self._rel_lng = util.TrueHeadingAndDistance(remaining_course,
                    rel_lng=self._rel_lng)
            fraction_remaining = remaining_distance / self._descent_distance 
            desired_altitude = (self.DesiredAltitude * (1 - fraction_remaining) +
                                self._descent_starting_altitude * fraction_remaining)
            altitude_error = self.CurrentAltitude - desired_altitude
            self._desired_climb_rate = util.rate_curve (altitude_error, self.DescentCurve)
            self._desired_climb_rate += self._nominal_descent_rate
            self._climbPitchPID.SetSetPoint (self._desired_climb_rate, self.ClimbRateAchievementSeconds)
            logger.log (5, "Descent Rate %g/%g--> %g", self.CurrentAltitude, desired_altitude, 
                    self._desired_climb_rate)
            ret = self._climbPitchPID.Compute (self.CurrentClimbRate, ms)
            logger.log (5, "Descent Pitch %g/%g--> %g", self.CurrentClimbRate, self._desired_climb_rate, 
                    ret)
        return ret

    def UpdateAirspeedPitch(self, ms):
        if self._airspeedPitchPID.GetMode() != PID.AUTOMATIC:
            if self.EngineOutPitchCurve:
                max_pitch = self.EngineOutPitchCurve[self._sensors.EnginesOut()]
                desired_pitch = max_pitch if max_pitch < self._desired_pitch else self._desired_pitch
                logger.debug("Activating airspeed pitch. max_pitch = %g, desired=%g, engines out = %d",
                        max_pitch, desired_pitch, self._sensors.EnginesOut())
            else:
                desired_pitch = self._desired_pitch
                logger.debug("Activating airspeed pitch. old desired=%g",
                        self._desired_pitch)
            self._airspeedPitchPID.SetMode (PID.AUTOMATIC,
                                        self.CurrentAirSpeed, -desired_pitch)
        self._set_pitch_pid_limits(self.PitchPIDLimits, self._airspeedPitchPID, True)
        self._airspeedPitchPID.SetSetPoint (self.MinClimbAirSpeed)
        desired_pitch = -self._airspeedPitchPID.Compute (self.CurrentAirSpeed, ms)
        if self._in_pid_optimization == "airspeed_pitch":
            self._pid_optimization_scoring.IncrementScore(self.CurrentAirSpeed, desired_pitch, self._pid_optimization_goal, self._journal_file)
        if self._journal_file and self.JournalPitch:
            self._journal_file.write(",%g,%g,%g"%(self.MinClimbAirSpeed, self.CurrentAirSpeed, desired_pitch))
        return desired_pitch

    def UpdateClimbratePitch(self, ms):
        alt_err = self.DesiredAltitude - self.CurrentAltitude
        if abs(alt_err) < self.ClimbPitchCurve[0][0]:
            if self._climbPitchPID.GetMode() != PID.AUTOMATIC:
                self._climbPitchPID.SetMode (PID.AUTOMATIC,
                                            self.CurrentClimbRate, self._desired_pitch)
            self._set_pitch_pid_limits(self.PitchPIDLimits, self._climbPitchPID)
            if self._in_pid_optimization != "climb_pitch":
                self._climbPitchPID.SetSetPoint (self._desired_climb_rate,
                        self.ClimbRateAchievementSeconds)
            desired_pitch = self._climbPitchPID.Compute (self.CurrentClimbRate, ms)
            logger.log (5, "Climb Pitch PID: %g/%g-->%g",
                    self.CurrentClimbRate, self._desired_climb_rate, desired_pitch)
            if self._in_pid_optimization == "climb_pitch":
                self._pid_optimization_scoring.IncrementScore(self.CurrentClimbRate, desired_pitch, self._pid_optimization_goal, self._journal_file)
            if self._journal_file and self.JournalPitch:
                self._journal_file.write(",%g,%g,%g"%(self._desired_climb_rate, self.CurrentClimbRate, desired_pitch))
        else:
            if self._climbPitchPID.GetMode() != PID.MANUAL:
                self._climbPitchPID.SetMode (PID.MANUAL,
                                            self.CurrentClimbRate, self._desired_pitch)
            desired_pitch = util.rate_curve(alt_err, self.ClimbPitchCurve)
            mn,mx = self._find_pitch_limits(self.PitchPIDLimits)
            if desired_pitch < mn:
                desired_pitch = mn
            elif desired_pitch > mx:
                desired_pitch = mx
            logger.log (5, "Climb Pitch Curve: %g-->%g", alt_err, desired_pitch)
        return desired_pitch

    def _set_pitch_pid_limits(self, absolute_limits, pid, invert=False):
        amn,amx = self._find_pitch_limits(absolute_limits)
        if invert:
            temp = -amx
            amx = -amn
            amn = temp
        rmn = amn
        rmx = amx
        logger.log (5, "pitch limits are %g,%g", rmn, rmx)
        pid.SetOutputLimits(rmn, rmx)

    def _find_pitch_limits(self, absolute_limits):
        if isinstance(absolute_limits,tuple):
            return absolute_limits
        roll = abs(self._sensors.Roll())
        max_pitch = util.rate_curve(roll, absolute_limits)
        return -max_pitch,max_pitch

    def Stop(self):
        self._throttlePID.SetMode (PID.MANUAL, self.CurrentAirSpeed, self._throttle_control.GetCurrent())
        self._climbPitchPID.SetMode (PID.MANUAL, self.CurrentAirSpeed, self._desired_pitch)
        self._airspeedPitchPID.SetMode (PID.MANUAL, self.CurrentClimbRate, self.DesiredAirSpeed)
        self._attitude_control.StopFlight()
        if self._journal_file:
            self._journal_file.close()
        return self._desired_pitch

    def Update(self):
        ms = util.millis(self._sensors.Time())
        self.CurrentAirSpeed = self._sensors.AirSpeed()
        self.CurrentTrueHeading = self._sensors.TrueHeading()
        self.CurrentAltitude = self._sensors.Altitude()
        heading_nominal = False
        if self._flight_mode == SUBMODE_TURN:
            self._desired_roll,_ = self.compute_roll(self.DesiredTrueHeading)
            if self.completed_turn():
                self.get_next_directive()
                return
        else:
            self.CurrentPosition = self._sensors.Position()
            if self._flight_mode == SUBMODE_SWOOP_DOWN:
                roll = 0
                if self.CurrentAltitude < self.DesiredAltitude + self.SwoopAltitudeReversal:
                    self._flight_mode = SUBMODE_SWOOP_UP
                    self._desired_pitch = self.SwoopPitch
                    self.DesiredAltitude = self._swoop_high_alt
                    self._throttle_control.Set (.75)
                    self._throttlePID.SetMode (PID.MANUAL, self.CurrentAirSpeed,
                            self._throttle_control.GetCurrent())
                elif self.CurrentAirSpeed > self.SwoopMaxAirSpeed:
                    # Going too fast. Better throttle down
                    self._throttlePID.SetMode (PID.AUTOMATIC, self.CurrentAirSpeed,
                            self._throttle_control.GetCurrent())
                    self.DesiredAirSpeed = self.SwoopMaxAirSpeed
            elif self._flight_mode == SUBMODE_SWOOP_UP:
                roll = 0
                if self.CurrentAltitude > self.DesiredAltitude - self.SwoopAltitudeReversal:
                    self._throttlePID.SetMode (PID.AUTOMATIC, self.CurrentAirSpeed,
                            self._throttle_control.GetCurrent())
                    self._flight_mode = self._mode_before_swoop
                    self.DesiredAirSpeed = self._airspeed_before_swoop
                    self.CurrentClimbRate = self._sensors.ClimbRate()
                    self._climbPitchPID.SetMode (PID.AUTOMATIC,
                                            self.CurrentClimbRate, self._sensors.Pitch())
                elif self.CurrentAirSpeed < self._swoop_min_as:
                    self._throttle_control.Set(1.0)
                    self._desired_pitch = self.SwoopPitch / 2.0
            elif self._flight_mode != SUBMODE_COURSE:
                roll,heading_error = self.compute_roll(self.DesiredTrueHeading)
                if abs(heading_error) < 5:
                    heading_nominal = True
            else:
                if self._flight_mode == SUBMODE_COURSE and self.completed_course():
                    self.get_next_directive()
                    return
                roll = self.compute_roll_from_course()
            if abs(roll) > self.MaxRoll:
                roll = self.MaxRoll if roll > 0 else -self.MaxRoll
            self._desired_roll = roll

        airspeed_nominal = False
        if self._journal_file:
            self._journal_file.write(str(ms))
        if self._throttlePID.GetMode() == PID.AUTOMATIC:
            if self._in_pid_optimization != "throttle":
                self._throttlePID.SetSetPoint (self.DesiredAirSpeed,
                        self._airspeed_achievement_minutes * 60.0)
            th = self._throttlePID.Compute (self.CurrentAirSpeed, ms)
            if self._in_pid_optimization == "throttle":
                self._pid_optimization_scoring.IncrementScore(self.CurrentAirSpeed, th, self._pid_optimization_goal, self._journal_file)
            if self._journal_file and self.JournalThrottle:
                self._journal_file.write(",%g,%g,%g"%(self.DesiredAirSpeed, self.CurrentAirSpeed, th))
            self._throttle_control.Set(th)
            if abs(self.CurrentAirSpeed - self.DesiredAirSpeed) / self.DesiredAirSpeed < .05:
                airspeed_nominal = True

        self._desired_climb_rate = self.get_climb_rate()
        logger.log(2, "Desired Climb Rate %g - %g ==> %g", self.DesiredAltitude, self.CurrentAltitude, self._desired_climb_rate)

        altitude_nominal = False
        if self._flight_mode != SUBMODE_SWOOP_DOWN and self._flight_mode != SUBMODE_SWOOP_UP:
            self.CurrentClimbRate = self._sensors.ClimbRate()
            self._desired_pitch = self.SetPitch(ms)
            if abs(self.CurrentAltitude - self.DesiredAltitude) < 50:
                altitude_nominal = True

        if self._journal_file:
            self._journal_file.write("\n")
            self._journal_flush_count += 1
            if self._journal_flush_count >= 10:
                self._journal_file.flush()
                self._journal_flush_count = 0

        pitch_diff = self._desired_pitch - self._last_pitch
        time_diff = ms - self._last_update_time
        if abs(pitch_diff) > self.MaxPitchChangePerSample * time_diff / self.PitchPIDSampleTime:
            pitch_diff = self.MaxPitchChangePerSample * time_diff / self.PitchPIDSampleTime
            if self._desired_pitch < self._last_pitch:
                pitch_diff *= -1
            self._last_pitch += pitch_diff
        else:
            self._last_pitch = self._desired_pitch
        self._attitude_control.UpdateControls (self._last_pitch, self._desired_roll, 0)
        self._last_update_time = ms
        if self._notify_when_nominal and altitude_nominal and airspeed_nominal and heading_nominal:
            self._notify_when_nominal = False
            self.get_next_directive()

    def NotifyWhenNominal(self):
        self._notify_when_nominal = True

    def get_climb_rate(self):
        altitude_error = self.DesiredAltitude - self.CurrentAltitude
        if self.ClimbRateCurve != None:
            ret = util.rate_curve(altitude_error, self.ClimbRateCurve)
            logger.log (5, "Climb rate curve %g ==> %g", altitude_error, ret)
        else:
            climb_rate = altitude_error / self.AltitudeAchievementMinutes
            limits = self.ClimbRateLimits
            if climb_rate < limits[0]:
                climb_rate = limits[0]
            elif climb_rate > limits[1]:
                climb_rate = limits[1]
            ret = climb_rate
        return ret

    # Compute a good self._desired_heading_rate_change based on the course, speed, heading and position
    def compute_roll_from_course(self):
        pos = self._sensors.Position()
        pos = util.AddPosition(pos, self._sensors.GroundSpeed() * self.CourseProjectionSeconds / 3600,
                self._sensors.GroundTrack())
        turn_radius = (360.0 * (self.CurrentAirSpeed / 60.0) / self.TurnRate) / (4 * util.M_PI)
        turn_radius *= self.InterceptMultiplier
        intercept_heading = util.CourseHeading(pos, self.DesiredCourse, turn_radius)
        roll,_ = self.compute_roll(intercept_heading)
        return roll

    def compute_roll(self, intercept_heading):
        heading_error = intercept_heading - self._sensors.GroundTrack()
        # Handle wrap around
        if heading_error > 180:
            heading_error -= 360
        elif heading_error < -180:
            heading_error += 360
        if self._force_turn_direction < 0:
            if heading_error <= 0:
                self._force_turn_direction = 0
            else:
                while heading_error >= 0:
                    heading_error -= 360
        elif self._force_turn_direction > 0:
            if heading_error >= 0:
                self._force_turn_direction = 0
            else:
                while heading_error <= 0:
                    heading_error += 360
        # heading rate change in degrees per second
        ret = util.rate_curve (heading_error, self.RollCurve)
        #logger.debug ("compute_roll: %g/%g-->%g error -->%g roll", self._sensors.GroundTrack(),
        #        intercept_heading, heading_error, ret)
        return ret, heading_error

    def FollowCourse(self, course, alt):
        self._flight_mode = SUBMODE_COURSE
        self.DesiredCourse = course
        self.DesiredAltitude = alt
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"

    def TurnTo(self, heading, roll_angle = 0, alt = 0):
        logger.debug ("Flight control turning to %g, roll %g, alt %d", heading, roll_angle, alt)
        self._flight_mode = SUBMODE_TURN
        self._force_turn_direction = roll_angle
        self.DesiredTrueHeading = heading
        if alt > 0:
            self.DesiredAltitude = alt
        logger.debug("starting turn at %g", self._sensors.Heading())
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"

    def Turn(self, degrees, roll_angle=0, alt=0):
        logger.debug ("Flight control turning %g, roll %g, alt %d", degrees, roll_angle, alt)
        self._flight_mode = SUBMODE_TURN
        self._force_turn_direction = roll_angle
        if alt > 0:
            self.DesiredAltitude = alt
        turn_start = self._sensors.GroundTrack()
        self.DesiredTrueHeading = turn_start + degrees
        logger.debug("starting turn at %g", turn_start)
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"

    def Swoop(self, low_alt, high_alt, min_airspeed=0):
        current_altitude = self._sensors.Altitude()
        self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes
        if current_altitude <= low_alt:
            self.DesiredAltitude = high_alt
        else:
            self._mode_before_swoop = self._flight_mode 
            self._airspeed_before_swoop = self.DesiredAirSpeed 
            self._flight_mode = SUBMODE_SWOOP_DOWN
            self._desired_pitch = -self.SwoopPitch
            # set altitude PID limits and desired speed higher for a swoop
            self.DesiredAirSpeed = self._callback.MaxAirSpeed
            # use a constant throttle setting to start
            self._throttlePID.SetMode (PID.MANUAL, self.CurrentAirSpeed, self._throttle_control.GetCurrent())
            self._climbPitchPID.SetMode (PID.MANUAL,
                                            self.CurrentClimbRate, self._desired_pitch)
            self._throttle_control.Set (.75)

            self._swoop_high_alt = high_alt
            self.DesiredAltitude = low_alt
        if min_airspeed > self.MinClimbAirSpeed:
            self._swoop_min_as = min_airspeed
        else:
            self._swoop_min_as = self.MinClimbAirSpeed
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"

    def FlyTo(self, coordinate, desired_altitude=0, desired_airspeed=0, rounding=True):
        self.DesiredCourse = [self._sensors.Position(), coordinate]
        if desired_altitude:
            self.DesiredAltitude = desired_altitude
        if desired_airspeed:
            self.DesiredAirSpeed = desired_airspeed
            self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes
        self._flight_mode = SUBMODE_COURSE
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"
        self._course_rounding = rounding
        logger.debug("Flight Control flying to (%g,%g)", coordinate[0], coordinate[1])

    def FlyCourse(self, course, desired_altitude=0, desired_airspeed=0, rounding=True):
        self.DesiredCourse = course
        if desired_altitude:
            self.DesiredAltitude = desired_altitude
        if desired_airspeed:
            self.DesiredAirSpeed = desired_airspeed
            self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes
        self._flight_mode = SUBMODE_COURSE
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"
        self._course_rounding = rounding
        logger.debug("Flight Control flying course (%g,%g) to (%g,%g), altitude=%g, airspeed=%g",
                course[0][0], course[0][1],
                course[1][0], course[1][1], self.DesiredAltitude, self.DesiredAirSpeed)

    def StraightAndLevel(self, desired_altitude=0, desired_airspeed=0, desired_heading = None):
        self._flight_mode = SUBMODE_STRAIGHT
        if desired_altitude:
            self.DesiredAltitude = desired_altitude
        else:
            self.DesiredAltitude = self._sensors.Altitude()
        if desired_airspeed:
            self.DesiredAirSpeed = desired_airspeed
            self._airspeed_achievement_minutes = self.AirSpeedAchievementMinutes
        if desired_heading == None:
            self.DesiredTrueHeading = self._sensors.TrueHeading()
        else:
            self.DesiredTrueHeading = desired_heading
        self._pitch_mode = None     # None == normal, otherwise "controlled_descent"
        logger.debug ("Flight Control Straight and Level at %g feet, airspeed=%g, heading%g",
                self.DesiredAltitude, self.DesiredAirSpeed, self.DesiredTrueHeading)

    def DescentCourse(self, course, desired_altitude, desired_airspeed=0, rounding=True):
        self.DesiredCourse = course
        self.DesiredAltitude = desired_altitude
        if desired_airspeed:
            self.DesiredAirSpeed = desired_airspeed
        self._flight_mode = SUBMODE_COURSE
        self._pitch_mode = "controlled_descent"
        self._course_rounding = rounding
        self._descent_starting_altitude = self.CurrentAltitude
        full_course = [self.CurrentPosition, course[1]]
        _,self._descent_distance, self._rel_lng = util.TrueHeadingAndDistance (full_course)
        hours_to_dest = self._descent_distance / self.DesiredAirSpeed
        self._airspeed_achievement_minutes = hours_to_dest * 60.0
        full_descent =  self.DesiredAltitude - self._descent_starting_altitude
        self._nominal_descent_rate = full_descent / (hours_to_dest * 60.0)
        logger.debug("Starting Descent course: distance=%g, descent of %g feet, nominal rate = %g",
                self._descent_distance, full_descent,
                self._nominal_descent_rate)

    def SetCallback(self, cb):
        self._callback = cb

    def PIDOptimizationStart(self, which_pid, params, scoring_object, outfile):
        self._flight_mode = SUBMODE_STRAIGHT
        self._in_pid_optimization = which_pid
        self._pid_optimization_scoring = scoring_object
        routing_to = self._in_pid_optimization.split('.')[0]
        if routing_to == "attitude":
            return self._attitude_control.PIDOptimizationStart (which_pid[9:], params, scoring_object, outfile)
        else:
            self._journal_file = outfile
            self._journal_flush_count = 0
            self.JournalPitch = False
            self.JournalThrottle = False
            if self._in_pid_optimization == "climb_pitch":
                self._climbPitchPID.SetTunings (params['P'], params['I'], params['D'])
                step = self._pid_optimization_scoring.InitializeScoring(self._desired_pitch)
                self._pid_optimization_goal = step.input_value[2]
                self._climbPitchPID.SetSetPoint (self._pid_optimization_goal, self.ClimbRateAchievementSeconds)
            elif self._in_pid_optimization == "airspeed_pitch":
                self._airspeedPitchPID.SetTunings (params['P'], params['I'], params['D'])
                step = self._pid_optimization_scoring.InitializeScoring(self._desired_pitch)
                self._pid_optimization_goal = step.input_value[2]
                self._airspeedPitchPID.SetSetPoint (self._pid_optimization_goal, self._airspeed_achievement_minutes * 60.0)
            elif self._in_pid_optimization == "throttle":
                self._throttlePID.SetTunings (params['P'], params['I'], params['D'])
                step = self._pid_optimization_scoring.InitializeScoring(self._throttle_control.GetCurrent())
                self._pid_optimization_goal = step.input_value[2]
                self._throttlePID.SetSetPoint (self._pid_optimization_goal)
            else:
                raise RuntimeError ("Unknown PID optimization target: %s"%which_pid)
            return step

    def PIDOptimizationNext(self):
        routing_to = self._in_pid_optimization.split('.')[0]
        if routing_to == "attitude":
            return self._attitude_control.PIDOptimizationNext()
        else:
            step = self._pid_optimization_scoring.GetNextStep()
            if not step:
                self.PIDOptimizationStop()
                return self._pid_optimization_scoring.GetScore()
            self._pid_optimization_goal = step.input_value
            if self._in_pid_optimization == "climb_pitch":
                self._climbPitchPID.SetSetPoint (self._pid_optimization_goal, self.ClimbRateAchievementSeconds)
            elif self._in_pid_optimization == "airspeed_pitch":
                self._airspeedPitchPID.SetSetPoint (self._pid_optimization_goal)
            elif self._in_pid_optimization == "throttle":
                self._throttlePID.SetSetPoint (self._pid_optimization_goal, self._airspeed_achievement_minutes * 60.0)
            else:
                raise RuntimeError ("Unknown PID optimization target")
            return step

    def PIDOptimizationStop(self):
        if self._in_pid_optimization == "climb_pitch":
            kp,ki,kd = self.ClimbPitchPIDTuningParams
            self._climbPitchPID.SetTunings (kp,ki,kd)
        elif self._in_pid_optimization == "airspeed_pitch":
            kp,ki,kd = self.AirspeedPitchPIDTuningParams
            self._airspeedPitchPID.SetTunings (kp,ki,kd)
        elif self._in_pid_optimization == "throttle":
            kp,ki,kd = self.ThrottlePIDTuningParams
            self._throttlePID.SetTunings (kp,ki,kd)
        else:
            raise RuntimeError ("Unknown PID optimization target")
        self._in_pid_optimization = ""
        if self._journal_file:
            self._journal_file.close()
            self._journal_file = None

    def completed_turn(self):
        diff = self.CurrentTrueHeading - self.DesiredTrueHeading
        if diff > 180:
            diff -= 360
        elif diff < -180:
            diff += 360
        return (abs(diff) < 3.0)

    def completed_course(self):
        # The course is considered completed if the aircraft has crossed the line
        # perpendicular to the course and intersecting the terminal waypoint

        # Translate the current position to a coordinate space with the terminal point at origin
        angle,distance,rel_lng = util.TrueHeadingAndDistance(self.DesiredCourse)
        lng,lat = self.CurrentPosition
        tlng,tlat = self.DesiredCourse[1]
        dlng = lng-tlng
        dlat = lat-tlat
        dlng *= rel_lng
        x,y = util.rotate2d (angle * util.M_PI / 180, dlng, dlat)
        #util.log_occasional_info("completed_course",
        #        "cur_pos = (%g,%g), to=(%g,%g), dlng=%g, dlat=%g, angle=%g, xy=%g,%g"%(
        #            self.CurrentPosition[0], self.CurrentPosition[1],
        #            self.DesiredCourse[1][0], self.DesiredCourse[1][1],
        #            dlng, dlat, angle, x, y))

        # turn_circumference = airspeed(nm / minute) / turn_rate (degrees / minute) * 360 degrees
        # turn_diameter = turn_circumference / (2 * PI)
        # turn_radius = turn_diameter / 2

        if self._course_rounding:
            turn_radius = (360.0 * (self._sensors.GroundSpeed() / 60.0) / self.TurnRate) / (4 * util.M_PI)
        else:
            turn_radius = 0
        if y >= -turn_radius / 60.0:
            logger.debug("Flight Control completed course")
            return True
        else:
            return False

    def get_next_directive(self):
        self._callback.GetNextDirective()
