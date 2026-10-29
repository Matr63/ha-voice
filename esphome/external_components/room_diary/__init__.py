import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_PORT
from esphome.components import microphone, switch as switch_comp

CODEOWNERS = ["@local"]
DEPENDENCIES = ["microphone", "network"]

room_diary_ns = cg.esphome_ns.namespace("room_diary")
RoomDiary = room_diary_ns.class_("RoomDiary", cg.Component)

CONF_HOST = "host"
CONF_MICROPHONE = "microphone"
CONF_ENABLE_SWITCH = "enable_switch"
CONF_RMS_THRESHOLD = "rms_threshold"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(RoomDiary),
        cv.Required(CONF_HOST): cv.string,
        cv.Required(CONF_PORT): cv.port,
        cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        cv.Optional(CONF_ENABLE_SWITCH): cv.use_id(switch_comp.Switch),
        cv.Optional(CONF_RMS_THRESHOLD, default=400): cv.int_range(min=1, max=20000),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic = await cg.get_variable(config[CONF_MICROPHONE])
    cg.add(var.set_microphone(mic))
    cg.add(var.set_host(config[CONF_HOST]))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_rms_threshold(config[CONF_RMS_THRESHOLD]))
    if CONF_ENABLE_SWITCH in config:
        sw = await cg.get_variable(config[CONF_ENABLE_SWITCH])
        cg.add(var.set_enable_switch(sw))
