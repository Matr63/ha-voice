import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import microphone, speaker
from esphome.const import CONF_ID, CONF_PORT

CODEOWNERS = ["@Matr63"]
DEPENDENCIES = ["microphone", "network"]
AUTO_LOAD = ["audio", "ring_buffer"]

ha_voice_sat_ns = cg.esphome_ns.namespace("ha_voice_sat")
HaVoiceSat = ha_voice_sat_ns.class_("HaVoiceSat", cg.Component)

SetListeningAction = ha_voice_sat_ns.class_("SetListeningAction", automation.Action)
SendEventAction = ha_voice_sat_ns.class_("SendEventAction", automation.Action)
IsConnectedCondition = ha_voice_sat_ns.class_("IsConnectedCondition", automation.Condition)

CONF_HOST = "host"
CONF_MICROPHONE = "microphone"
CONF_SPEAKER = "speaker"
CONF_LISTENING = "listening"
CONF_DEVICE_NAME = "device_name"
CONF_ON_STATE = "on_state"
CONF_ON_VOLUME = "on_volume"
CONF_ON_SETTING = "on_setting"
CONF_ON_TTS_START = "on_tts_start"
CONF_ON_TTS_END = "on_tts_end"
CONF_ON_CONNECTED = "on_connected"
CONF_ON_DISCONNECTED = "on_disconnected"
CONF_EVENT = "event"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(HaVoiceSat),
        cv.Optional(CONF_HOST, default="homeassistant.local"): cv.string,
        cv.Optional(CONF_PORT, default=10500): cv.port,
        cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        cv.Optional(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        cv.Optional(CONF_LISTENING, default=True): cv.boolean,
        cv.Optional(CONF_DEVICE_NAME, default=""): cv.string,
        cv.Optional(CONF_ON_STATE): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_VOLUME): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_SETTING): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_TTS_START): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_TTS_END): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_CONNECTED): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_DISCONNECTED): automation.validate_automation(single=True),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic = await cg.get_variable(config[CONF_MICROPHONE])
    cg.add(var.set_microphone(mic))
    if CONF_SPEAKER in config:
        spk = await cg.get_variable(config[CONF_SPEAKER])
        cg.add(var.set_speaker(spk))
    cg.add(var.set_host(config[CONF_HOST]))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_listening(config[CONF_LISTENING]))
    cg.add(var.set_device_name(config[CONF_DEVICE_NAME]))

    if CONF_ON_STATE in config:
        await automation.build_automation(
            var.get_state_trigger(), [(cg.int_, "x")], config[CONF_ON_STATE]
        )
    if CONF_ON_VOLUME in config:
        await automation.build_automation(
            var.get_volume_trigger(), [(cg.float_, "x")], config[CONF_ON_VOLUME]
        )
    if CONF_ON_SETTING in config:
        await automation.build_automation(
            var.get_setting_trigger(),
            [(cg.std_string, "key"), (cg.std_string, "value")],
            config[CONF_ON_SETTING],
        )
    for key, getter in (
        (CONF_ON_TTS_START, var.get_tts_start_trigger()),
        (CONF_ON_TTS_END, var.get_tts_end_trigger()),
        (CONF_ON_CONNECTED, var.get_connected_trigger()),
        (CONF_ON_DISCONNECTED, var.get_disconnected_trigger()),
    ):
        if key in config:
            await automation.build_automation(getter, [], config[key])


@automation.register_action(
    "ha_voice_sat.set_listening",
    SetListeningAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(HaVoiceSat),
            cv.Required(CONF_LISTENING): cv.templatable(cv.boolean),
        }
    ),
    synchronous=True,
)
async def set_listening_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    tmpl = await cg.templatable(config[CONF_LISTENING], args, bool)
    cg.add(var.set_listening(tmpl))
    return var


@automation.register_action(
    "ha_voice_sat.send_event",
    SendEventAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(HaVoiceSat),
            cv.Required(CONF_EVENT): cv.templatable(cv.string),
        }
    ),
    synchronous=True,
)
async def send_event_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    tmpl = await cg.templatable(config[CONF_EVENT], args, cg.std_string)
    cg.add(var.set_event(tmpl))
    return var


@automation.register_condition(
    "ha_voice_sat.is_connected",
    IsConnectedCondition,
    cv.Schema({cv.GenerateID(): cv.use_id(HaVoiceSat)}),
)
async def is_connected_to_code(config, condition_id, template_arg, args):
    var = cg.new_Pvariable(condition_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
