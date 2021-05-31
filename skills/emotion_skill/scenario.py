import sentry_sdk
import random
import logging
from os import getenv
from common.constants import MUST_CONTINUE, CAN_CONTINUE_SCENARIO
from common.link import link_to
from common.emotion import is_joke_requested, is_sad, is_alone, is_boring, \
    skill_trigger_phrases, talk_about_emotion, is_pain
from common.universal_templates import book_movie_music_found
from common.utils import get_emotions
from collections import defaultdict
import re

sentry_sdk.init(getenv('SENTRY_DSN'))
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class EmotionSkillScenario:
    def __init__(self, steps, jokes, advices, logger):
        self.emotion_precision = {'anger': 1, 'fear': 0.894, 'joy': 1,
                                  'love': 0.778, 'sadness': 1, 'surprise': 0.745, 'neutral': 0}
        self.steps = steps
        self.jokes = jokes
        self.advices = advices
        self.logger = logger
        self.regexp_sad = False

    def _get_user_emotion(self, annotated_user_phrase, discard_emotion=None):
        if is_sad(annotated_user_phrase['text']):
            self.regexp_sad = True
            return 'sadness'
        elif is_boring(annotated_user_phrase['text']):
            return 'neutral'
        most_likely_emotion = None
        emotion_probs = get_emotions(annotated_user_phrase, probs=True)
        if discard_emotion in emotion_probs:
            emotion_probs.pop(discard_emotion)
        most_likely_prob = max(emotion_probs.values())
        for emotion in emotion_probs.keys():
            if emotion_probs.get(emotion, 0) == most_likely_prob:
                most_likely_emotion = emotion
        return most_likely_emotion

    # def _check_for_repetition(self, reply, prev_replies_for_user):
    #     reply = reply.lower()
    #     lower_physical = physical_activites.lower()
    #     if reply in prev_replies_for_user:
    #         return True
    #     for prev_reply in prev_replies_for_user:
    #         if lower_physical in prev_reply and lower_physical in reply:
    #             return True
    #     return False

    def _check_i_feel(self, user_phrase, bot_phrase):
        result = bool(re.match("i ((feel)|(am feeling)|(am)) .*", user_phrase))
        result = result or 'how do you feel' in bot_phrase
        result = result or 'how are you' in bot_phrase
        return result

    def _random_choice(self, data, discard_data=None):
        discard_data = [] if discard_data is None else discard_data
        chosen_data = list(set(data).difference(set(discard_data)))
        if len(chosen_data):
            return random.choice(chosen_data)
        else:
            return ""

    # def _is_stop()

    def _get_reply_and_conf(self, user_phrase, bot_phrase, emotion,
                            emotion_skill_attributes, intent, human_attr):

        start_states = {
            "joy": 'joy_i_feel' if self._check_i_feel(user_phrase, bot_phrase)
            else 'joy_feeling_towards_smth',
            "sadness": 'sad_and_lonely',
            "fear": 'fear',
            "anger": 'anger',
            "surprise": 'surprise',
            "love": 'love'
        }
        state = emotion_skill_attributes.get("state", "")
        prev_jokes_advices = emotion_skill_attributes.get("prev_jokes_advices", [])
        is_yes = intent.get("yes", {}).get("detected", 0)
        is_no = intent.get("no", {}).get("detected", 0)
        just_asked_about_jokes = "why hearing jokes is so important for you? are you sad?" in bot_phrase
        reply, confidence = "", 0
        link = ''
        self.logger.info(
            f"_get_reply_and_conf {user_phrase}; {bot_phrase}; {emotion}; {just_asked_about_jokes};"
            f" {emotion_skill_attributes}; {intent}; {human_attr}"
        )

        if state == "":
            # start_state
            state = start_states[emotion]
            step = self.steps[state]
            reply = self._random_choice(step['answers'])
            confidence = min(0.98, self.emotion_precision[emotion])
            if len(step['next_step']):
                state = random.choice(step['next_step'])
        elif state == "sad_and_lonely" and just_asked_about_jokes and is_no:
            reply = "Actually, I love jokes but not during this competition. Dead serious about that."
            confidence = 1.0
            state = ''
        elif state == 'offered_advice':
            # we offered an advice
            if is_yes:
                # provide advices and offer another one
                reply = self._random_choice(self.advices[emotion], prev_jokes_advices)
                state = 'offer_another_advice'
                if reply == "":
                    state = "sad_and_lonely_end"
                    step = self.steps[state]
                    reply = random.choice(step['answers'])
                else:
                    prev_jokes_advices.append(reply)
                confidence = 1.0
            elif is_no:
                state = 'no'
                step = self.steps[state]
                reply = random.choice(step['answers'])
                if len(step['next_step']):
                    state = random.choice(step['next_step'])
                else:
                    state = ""
                confidence = 1.0
        else:
            step = self.steps[state]
            reply = random.choice(step['answers'])
            if len(step['next_step']):
                state = random.choice(step['next_step'])
            link = step['link']
            if link:
                link = link_to([link], human_attributes=human_attr)
                link['phrase'] = reply
                # reply += link['phrase']
            confidence = 1.0

        emotion_skill_attributes = {
            "state": state,
            "emotion": emotion,
            "prev_jokes_advices": prev_jokes_advices
        }

        return reply, confidence, link, emotion_skill_attributes

    def __call__(self, dialogs):
        texts = []
        confidences = []
        attrs = []
        human_attrs = []
        bot_attrs = []
        for dialog in dialogs:
            try:
                human_attributes = {}
                human_attributes["used_links"] = dialog["human"]["attributes"].get("used_links", defaultdict(list))
                human_attributes["disliked_skills"] = dialog["human"]["attributes"].get("disliked_skills", [])
                human_attributes["emotion_skill_attributes"] = dialog["human"]["attributes"].get(
                    "emotion_skill_attributes", {})
                emotion_skill_attributes = human_attributes["emotion_skill_attributes"]
                state = emotion_skill_attributes.get("state", "")
                emotion = emotion_skill_attributes.get("emotion", "")
                bot_attributes = {}
                attr = {"can_continue": CAN_CONTINUE_SCENARIO}
                annotated_user_phrase = dialog['utterances'][-1]
                user_phrase = annotated_user_phrase['text']
                most_likely_emotion = self._get_user_emotion(annotated_user_phrase)
                intent = annotated_user_phrase['annotations'].get("intent_catcher", {})
                prev_bot_phrase, prev_annotated_bot_phrase = '', {}
                if dialog['bot_utterances']:
                    prev_annotated_bot_phrase = dialog['bot_utterances'][-1]
                    prev_bot_phrase = prev_annotated_bot_phrase['text']
                very_confident = any([function(user_phrase)
                                      for function in [is_sad, is_boring, is_alone, is_joke_requested, is_pain]])
                # Confident if regezp
                link = ''
                if len(dialog['utterances']) > 1:
                    # Check if we were interrupted
                    active_skill = dialog['utterances'][-2]['active_skill'] == 'emotion_skill'
                    if not active_skill and state != "":
                        state = ""
                        emotion_skill_attributes['state'] = ""
                if emotion == "" or state == "":
                    emotion = most_likely_emotion
                if is_joke_requested(user_phrase):
                    state = "joke_requested"
                    emotion_skill_attributes['state'] = state
                elif is_alone(user_phrase):
                    state = "sad_and_lonely"
                    emotion_skill_attributes['state'] = state
                elif is_pain(annotated_user_phrase['text']):
                    state = "pain_i_feel"
                    emotion_skill_attributes['state'] = state
                logger.info(f"user sent: {annotated_user_phrase['text']} state: {state} emotion: {emotion}")
                if talk_about_emotion(annotated_user_phrase, prev_annotated_bot_phrase):
                    reply = f'OK. {random.choice(skill_trigger_phrases())}'
                    attr['can_continue'] = MUST_CONTINUE
                    confidence = 1
                elif emotion != "neutral" or state != "":
                    reply, confidence, link, emotion_skill_attributes = self._get_reply_and_conf(
                        annotated_user_phrase['text'],
                        prev_bot_phrase,
                        emotion,
                        emotion_skill_attributes,
                        intent,
                        human_attributes
                    )
                    human_attributes['emotion_skill_attributes'] = emotion_skill_attributes
                    if book_movie_music_found(annotated_user_phrase):
                        logging.info('Found named topic in user utterance - dropping confidence')
                        confidence = min(confidence, 0.9)
                else:
                    reply = ""
                    confidence = 0.0
                was_trigger = any([trigger_question in prev_bot_phrase
                                   for trigger_question in skill_trigger_phrases()])
                if dialog['bot_utterances']:
                    was_active = dialog['bot_utterances'][-1].get('active_skill', {}) == 'emotion_skill'
                    was_book_or_movie = dialog['bot_utterances'][-1].get('active_skill', {}) in ['book_skill',
                                                                                                 'dff_movie_skill']
                else:
                    was_active = False
                    was_book_or_movie = False
                if (was_trigger or was_active or self.regexp_sad) and not was_book_or_movie:
                    attr['can_continue'] = MUST_CONTINUE
                    confidence = 1
                elif was_book_or_movie:
                    confidence = 0.5 * confidence
                if not very_confident and not was_active:
                    confidence = min(confidence, 0.99)
                    attr['can_continue'] = CAN_CONTINUE_SCENARIO
            except Exception as e:
                self.logger.exception("exception in emotion skill")
                sentry_sdk.capture_exception(e)
                reply = ""
                state = ""
                confidence = 0.0
                human_attributes, bot_attributes, attr = {}, {}, {}
                link = ""
                annotated_user_phrase = {'text': ""}

            if link:
                if link["skill"] not in human_attributes["used_links"]:
                    human_attributes["used_links"][link["skill"]] = []
                human_attributes["used_links"][link["skill"]].append(link['phrase'])

            self.logger.info(f"__call__ reply: {reply}; conf: {confidence};"
                             f" user_phrase: {annotated_user_phrase['text']}"
                             f" human_attributes: {human_attributes}"
                             f" bot_attributes: {bot_attributes}"
                             f" attributes: {attr}")
            texts.append(reply)
            confidences.append(confidence)
            human_attrs.append(human_attributes)
            bot_attrs.append(bot_attributes)
            attrs.append(attr)

        return texts, confidences, human_attrs, bot_attrs, attrs
