from cereal import car
from selfdrive.config import Conversions as CV
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from selfdrive.car.interfaces import CarStateBase
from selfdrive.car.mazda.values import DBC, LKAS_LIMITS, GEN1, TI_STATE, CAR

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)

    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR"]["GEAR"]

    self.crz_btns_counter = 0
    self.acc_active_last = False
    self.lkas_allowed_speed = False

    self.ti_ramp_down = False
    self.ti_version = 1
    self.ti_state = TI_STATE.RUN
    self.ti_violation = 0
    self.ti_error = 0
    self.ti_lkas_allowed = False

  def update(self, cp, cp_cam):

    ret = car.CarState.new_message()
    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["FL"],
      cp.vl["WHEEL_SPEEDS"]["FR"],
      cp.vl["WHEEL_SPEEDS"]["RL"],
      cp.vl["WHEEL_SPEEDS"]["RR"],
    )
    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    # Match panda speed reading
    self.speed_kph = cp.vl["ENGINE_DATA"]["SPEED"]
    ret.standstill = self.speed_kph < .1

    can_gear = int(cp.vl["GEAR"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))

    ret.genericToggle = bool(cp.vl["BLINK_INFO"]["HIGH_BEAMS"])
    ret.leftBlindspot = cp.vl["BSM"]["LEFT_BS1"] == 1
    ret.rightBlindspot = cp.vl["BSM"]["RIGHT_BS1"] == 1
    ret.leftBlinker, ret.rightBlinker = self.update_blinker_from_lamp(40, cp.vl["BLINK_INFO"]["LEFT_BLINK"] == 1,
                                                                      cp.vl["BLINK_INFO"]["RIGHT_BLINK"] == 1)

    if self.CP.enableTorqueInterceptor:
      ret.steeringTorque = cp.vl["TI_FEEDBACK"]["TI_TORQUE_SENSOR"]

      self.ti_version = cp.vl["TI_FEEDBACK"]["VERSION_NUMBER"]
      self.ti_state = cp.vl["TI_FEEDBACK"]["STATE"] # DISCOVER = 0, OFF = 1, DRIVER_OVER = 2, RUN=3
      self.ti_violation = cp.vl["TI_FEEDBACK"]["VIOL"] # 0 = no violation
      self.ti_error = cp.vl["TI_FEEDBACK"]["ERROR"] # 0 = no error
      if self.ti_version > 1:
        self.ti_ramp_down = (cp.vl["TI_FEEDBACK"]["RAMP_DOWN"] == 1)

      ret.steeringPressed = abs(ret.steeringTorque) > LKAS_LIMITS.TI_STEER_THRESHOLD
      self.ti_lkas_allowed = not self.ti_ramp_down and self.ti_state == TI_STATE.RUN
    else:
      ret.steeringTorque = cp.vl["STEER_TORQUE"]["STEER_TORQUE_SENSOR"]
      ret.steeringPressed = abs(ret.steeringTorque) > LKAS_LIMITS.STEER_THRESHOLD

    ret.steeringAngleDeg = cp.vl["STEER"]["STEER_ANGLE"]      

    ret.steeringTorqueEps = cp.vl["STEER_TORQUE"]["STEER_TORQUE_MOTOR"]
    ret.steeringRateDeg = cp.vl["STEER_RATE"]["STEER_ANGLE_RATE"]

    # TODO: this should be from 0 - 1.
    ret.brakePressed = cp.vl["PEDALS"]["BRAKE_ON"] == 1
    ret.brake = cp.vl["BRAKE"]["BRAKE_PRESSURE"]

    ret.seatbeltUnlatched = cp.vl["SEATBELT"]["DRIVER_SEATBELT"] == 0
    ret.doorOpen = any([cp.vl["DOORS"]["FL"], cp.vl["DOORS"]["FR"],
                        cp.vl["DOORS"]["BL"], cp.vl["DOORS"]["BR"]])

    # TODO: this should be from 0 - 1.
    ret.gas = cp.vl["ENGINE_DATA"]["PEDAL_GAS"]
    ret.gasPressed = ret.gas > 0

    # Either due to low speed or hands off
    lkas_blocked = cp.vl["STEER_RATE"]["LKAS_BLOCK"] == 1

    # LKAS is enabled at 52kph going up and disabled at 45kph going down
    # wait for LKAS_BLOCK signal to clear when going up since it lags behind the speed sometimes
    if self.speed_kph > LKAS_LIMITS.ENABLE_SPEED:
      self.lkas_allowed_speed = True
    elif self.speed_kph < LKAS_LIMITS.DISABLE_SPEED:
      self.lkas_allowed_speed = False

    # TODO: the signal used for available seems to be the adaptive cruise signal, instead of the main on
    #       it should be used for carState.cruiseState.nonAdaptive instead
    ret.cruiseState.available = cp_cam.vl["CRZ_CTRL"]["CRZ_AVAILABLE"] == 1
    ret.cruiseState.enabled = cp.vl["CRZ_EVENTS"]["CRUISE_ACTIVE_CAR_MOVING"] == 1
    ret.cruiseState.speed = cp.vl["CRZ_EVENTS"]["CRZ_SPEED"] * CV.KPH_TO_MS

    # On if no driver torque the last 5 seconds
    if self.CP.carFingerprint != CAR.CX9_2021:
      ret.steerWarning = cp.vl["STEER_RATE"]["HANDS_OFF_5_SECONDS"] == 1
    else:
      ret.steerWarning = False

    self.acc_active_last = ret.cruiseState.enabled

    self.cam_lkas = cp_cam.vl["CAM_LKAS"]
    self.cam_laneinfo = cp_cam.vl["CAM_LANEINFO"]
    self.crz_btns_counter = cp.vl["CRZ_BTNS"]["CTR"]
    ret.steerError = False

    self.cp_cam = cp_cam
    self.cp = cp

    return ret

  @staticmethod
  def get_can_parser(CP):
    # this function generates lists for signal, messages and initial values
    signals = [
      # sig_name, sig_address, default
      ("LEFT_BLINK", "BLINK_INFO", 0),
      ("RIGHT_BLINK", "BLINK_INFO", 0),
      ("HIGH_BEAMS", "BLINK_INFO", 0),
      ("STEER_ANGLE", "STEER", 0),
      ("STEER_ANGLE_RATE", "STEER_RATE", 0),
      ("STEER_TORQUE_SENSOR", "STEER_TORQUE", 0),
      ("STEER_TORQUE_MOTOR", "STEER_TORQUE", 0),
      ("FL", "WHEEL_SPEEDS", 0),
      ("FR", "WHEEL_SPEEDS", 0),
      ("RL", "WHEEL_SPEEDS", 0),
      ("RR", "WHEEL_SPEEDS", 0),
    ]
    checks = [
      # sig_address, frequency
      ("BLINK_INFO", 10),
      ("STEER", 67),
      ("STEER_RATE", 83),
      ("STEER_TORQUE", 83),
      ("WHEEL_SPEEDS", 100),
    ]
    if CP.carFingerprint in GEN1:
      signals += [
        ("LKAS_BLOCK", "STEER_RATE", 0),
        ("LKAS_TRACK_STATE", "STEER_RATE", 0),
        ("HANDS_OFF_5_SECONDS", "STEER_RATE", 0),
        ("CRUISE_ACTIVE_CAR_MOVING", "CRZ_EVENTS", 0),
        ("CRZ_SPEED", "CRZ_EVENTS", 0),
        ("STANDSTILL", "PEDALS", 0),
        ("BRAKE_ON", "PEDALS", 0),
        ("BRAKE_PRESSURE", "BRAKE", 0),
        ("GEAR", "GEAR", 0),
        ("DRIVER_SEATBELT", "SEATBELT", 0),
        ("FL", "DOORS", 0),
        ("FR", "DOORS", 0),
        ("BL", "DOORS", 0),
        ("BR", "DOORS", 0),
        ("PEDAL_GAS", "ENGINE_DATA", 0),
        ("SPEED", "ENGINE_DATA", 0),
        ("CTR", "CRZ_BTNS", 0),
        ("LEFT_BS1", "BSM", 0),
        ("RIGHT_BS1", "BSM", 0),
      ]

      checks += [
        ("ENGINE_DATA", 100),
        
        ("CRZ_EVENTS", 50),
        ("CRZ_BTNS", 10),
        ("PEDALS", 50),
        ("BRAKE", 50),
        ("SEATBELT", 10),
        ("DOORS", 10),
        ("GEAR", 20),
        ("BSM", 10),
      ]
    # get real driver torque if we are using a torque interceptor
    if CP.enableTorqueInterceptor:
      signals += [
        ("TI_TORQUE_SENSOR", "TI_FEEDBACK", 0),
        ("CHKSUM", "TI_FEEDBACK", 0),
        ("VERSION_NUMBER", "TI_FEEDBACK", 0),
        ("STATE", "TI_FEEDBACK", 0),
        ("VIOL", "TI_FEEDBACK", 0),
        ("ERROR", "TI_FEEDBACK", 0),
        ("RAMP_DOWN", "TI_FEEDBACK", 0),
      ]

      checks += [
        ("TI_FEEDBACK", 100),
      ]
      
    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = []
    checks = []

    if CP.carFingerprint in GEN1:
      signals += [
        # sig_name, sig_address, default
        ("LKAS_REQUEST", "CAM_LKAS", 0),
        ("CTR", "CAM_LKAS", 0),
        ("ERR_BIT_1", "CAM_LKAS", 0),
        ("LINE_NOT_VISIBLE", "CAM_LKAS", 0),
        ("BIT_1", "CAM_LKAS", 1),
        ("ERR_BIT_2", "CAM_LKAS", 0),
        ("STEERING_ANGLE", "CAM_LKAS", 0),
        ("ANGLE_ENABLED", "CAM_LKAS", 0),
        ("CHKSUM", "CAM_LKAS", 0),

        ("LINE_VISIBLE", "CAM_LANEINFO", 0),
        ("LINE_NOT_VISIBLE", "CAM_LANEINFO", 1),
        ("LANE_LINES", "CAM_LANEINFO", 0),
        ("BIT1", "CAM_LANEINFO", 0),
        ("BIT2", "CAM_LANEINFO", 0),
        ("BIT3", "CAM_LANEINFO", 0),
        ("NO_ERR_BIT", "CAM_LANEINFO", 1),
        ("S1", "CAM_LANEINFO", 0),
        ("S1_HBEAM", "CAM_LANEINFO", 0),
      ]
      
      checks += [
        # sig_address, frequency
        ("CAM_LANEINFO", 2),
        ("CAM_LKAS", 16),
      ]

      signals += [
        ("CRZ_ACTIVE", "CRZ_CTRL", 0),
        ("CRZ_AVAILABLE", "CRZ_CTRL", 0),
        ("DISTANCE_SETTING", "CRZ_CTRL", 0),
        ("ACC_ACTIVE_2", "CRZ_CTRL", 0),
        ("DISABLE_TIMER_1", "CRZ_CTRL", 0),
        ("DISABLE_TIMER_2", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_1", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_2", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_3", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_4", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_5", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_6", "CRZ_CTRL", 0),
      ]
      signals += [
        ("STATUS", "CRZ_INFO", 0),
        ("STATIC_1", "CRZ_INFO", 0),
        ("ACCEL_CMD", "CRZ_INFO", 0),
        ("CRZ_ENDED", "CRZ_INFO", 0),
        ("ACC_SET_ALLOWED", "CRZ_INFO", 0),
        ("ACC_ACTIVE", "CRZ_INFO", 0),
        ("MYSTERY_BIT", "CRZ_INFO", 0),
        ("CTR1", "CRZ_INFO", 0),
        ("CHECKSUM", "CRZ_INFO", 0),
      ]
    
      for addr in range(361,367):
        msg = f"RADAR_{addr}"
        signals += [
          ("MSGS_1", msg, 0),
          ("MSGS_2", msg, 0),
          ("CTR", msg, 0),
        ]
        checks += [(msg, 10)]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2)
