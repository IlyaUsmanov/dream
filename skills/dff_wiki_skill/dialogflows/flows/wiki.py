import logging
import os
import random
import re
import requests
import en_core_web_sm
import nltk
import sentry_sdk

from deeppavlov import build_model

import common.constants as common_constants
import common.dialogflow_framework.stdm.dialogflow_extention as dialogflow_extention
import common.dialogflow_framework.utils.state as state_utils
from common.universal_templates import COMPILE_NOT_WANT_TO_TALK_ABOUT_IT, COMPILE_LETS_TALK
from common.utils import is_no, is_yes
from common.wiki_skill import used_types_dict
from common.wiki_skill import choose_title, find_all_titles, find_paragraph, find_all_paragraphs, delete_hyperlinks
from common.wiki_skill import find_entity_wp, find_entity_nounphr, if_user_dont_know_topic
from common.wiki_skill import QUESTION_TEMPLATES

import dialogflows.scopes as scopes
from dialogflows.flows.wiki_states import State as WikiState

sentry_sdk.init(os.getenv('SENTRY_DSN'))
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.DEBUG)
logger = logging.getLogger(__name__)

nlp = en_core_web_sm.load()

config_name = os.getenv("CONFIG")
text_qa_url = os.getenv("TEXT_QA_URL")

ANSWER_CONF_THRES = 0.95

page_extractor = build_model(config_name, download=True)

titles_by_type = {}
for elem in used_types_dict:
    types = elem.get("types", [])
    titles = elem["titles"]
    for tp in types:
        titles_by_type[tp] = titles

titles_by_entity_substr = {}
page_titles_by_entity_substr = {}
for elem in used_types_dict:
    entity_substrings = elem.get("entity_substr", [])
    titles = elem["titles"]
    page_title = elem.get("page_title", "")
    for substr in entity_substrings:
        titles_by_entity_substr[substr] = titles
        if page_title:
            page_titles_by_entity_substr[substr] = page_title

CONF_1 = 1.0
CONF_2 = 0.98
CONF_3 = 0.95
CONF_4 = 0.9
CONF_5 = 0.0

found_pages_dict = {}


def find_entity(vars, where_to_find="current"):
    if where_to_find == "current":
        annotations = state_utils.get_last_human_utterance(vars)["annotations"]
        found_entity_substr, found_entity_id, found_entity_types = find_entity_wp(annotations)
        if not found_entity_substr:
            found_entity_substr = find_entity_nounphr(annotations)
    else:
        all_user_uttr = vars["agent"]["dialog"]["human_utterances"]
        utt_num = len(all_user_uttr)
        found_entity_substr = ""
        found_entity_types = []
        found_entity_id = ""
        if utt_num > 1:
            for i in range(utt_num - 2, 0, -1):
                annotations = all_user_uttr[i]["annotations"]
                found_entity_substr, found_entity_id, found_entity_types = find_entity_wp(annotations)
                if not found_entity_substr:
                    found_entity_substr = find_entity_nounphr(annotations)
                if found_entity_substr:
                    break
    logger.info(f"find_entity, substr {found_entity_substr} types {found_entity_types}")
    return found_entity_substr, found_entity_id, found_entity_types


def get_page_title(vars, entity_substr):
    found_page = ""
    if entity_substr in page_titles_by_entity_substr:
        found_page = page_titles_by_entity_substr[entity_substr]
    else:
        annotations = state_utils.get_last_human_utterance(vars)["annotations"]
        el = annotations.get("entity_linking", [])
        for entity in el:
            if isinstance(entity, dict) and entity["entity_substr"] == entity_substr:
                found_pages_titles = entity["entity_pages_titles"]
                if found_pages_titles:
                    found_page = found_pages_titles[0]
    logger.info(f"found_page {found_page}")
    return found_page


def get_page_content(page_title):
    page_content = {}
    main_pages = {}
    try:
        if page_title:
            page_content_batch, main_pages_batch = page_extractor([[page_title]])
            if page_content_batch and page_content_batch[0]:
                page_content = page_content_batch[0][0]
                main_pages = main_pages_batch[0][0]
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logger.exception(e)

    return page_content, main_pages


def get_page_info(vars, where_to_find="current"):
    shared_memory = state_utils.get_shared_memory(vars)
    curr_pages = shared_memory.get("curr_pages", [])
    found_entity_substr_list = shared_memory.get("found_entity_substr", [])
    prev_title = shared_memory.get("prev_title", "")
    prev_page_title = shared_memory.get("prev_page_title", "")
    used_titles = shared_memory.get("used_titles", [])
    found_entity_types_list = shared_memory.get("found_entity_types", [])
    new_page = shared_memory.get("new_page", False)
    page_content_list = []
    main_pages_list = []
    if curr_pages:
        for page in curr_pages[-2:]:
            page_content, main_pages = get_page_content(page)
            page_content_list.append(page_content)
            main_pages_list.append(main_pages)
    else:
        found_entity_substr, _, found_entity_types = find_entity(vars, where_to_find)
        found_entity_substr_list.append(found_entity_substr)
        found_entity_types_list.append(list(found_entity_types))
        curr_page = get_page_title(vars, found_entity_substr)
        if curr_page:
            curr_pages.append(curr_page)
        for page in curr_pages[-2:]:
            page_content, main_pages = get_page_content(page)
            page_content_list.append(page_content)
            main_pages_list.append(main_pages)
    return found_entity_substr_list, prev_title, prev_page_title, found_entity_types_list, used_titles, curr_pages, \
        page_content_list, main_pages_list, new_page


def get_titles(found_entity_substr, found_entity_types, page_content):
    all_titles = find_all_titles([], page_content)
    titles_we_use = []
    titles_q = {}
    for tp in found_entity_types:
        tp_titles = titles_by_type.get(tp, {})
        titles_we_use += list(tp_titles.keys())
        titles_q = {**titles_q, **tp_titles}
    substr_titles = titles_by_entity_substr.get(found_entity_substr, {})
    titles_we_use += list(substr_titles.keys())
    titles_q = {**titles_q, **substr_titles}
    return titles_q, titles_we_use, all_titles


def get_title_info(found_entity_substr, found_entity_types, prev_title, used_titles, page_content):
    all_titles = find_all_titles([], page_content)
    titles_we_use = []
    for tp in found_entity_types:
        titles_we_use += list(titles_by_type.get(tp, {}).keys())
    titles_we_use += list(titles_by_entity_substr.get(found_entity_substr, {}).keys())

    logger.info(f"all_titles {all_titles}")
    chosen_title, chosen_page_title = choose_title(vars, all_titles, titles_we_use, prev_title, used_titles)
    return chosen_title, chosen_page_title


def make_facts_str(paragraphs):
    facts_str = ""
    mentions_list = []
    mention_pages_list = []
    paragraph = ""
    logger.info(f"paragraphs {paragraphs}")
    if paragraphs:
        paragraph = paragraphs[0]
    sentences = nltk.sent_tokenize(paragraph)
    sentences_list = []
    cur_len = 0
    max_len = 50
    for sentence in sentences:
        sanitized_sentence, mentions, mention_pages = delete_hyperlinks(sentence)
        words = nltk.word_tokenize(sanitized_sentence)
        if cur_len + len(words) < max_len:
            sentences_list.append(sanitized_sentence)
            cur_len += len(words)
            mentions_list += mentions
            mention_pages_list += mention_pages
    if sentences_list:
        facts_str = " ".join(sentences_list)
    cur_len = 0
    if sentences and not sentences_list:
        sentence = sentences[0]
        sanitized_sentence, mentions, mention_pages = delete_hyperlinks(sentence)
        sentence_parts = sanitized_sentence.split(", ")
        mentions_list += mentions
        mention_pages_list += mention_pages
        for part in sentence_parts:
            words = nltk.word_tokenize(part)
            if cur_len + len(words) < max_len:
                sentences_list.append(part)
                cur_len += len(words)
            facts_str = ", ".join(sentences_list)
            if facts_str and not facts_str.endswith("."):
                facts_str = f"{facts_str}."
    logger.info(f"mentions {mentions_list} mention_pages {mention_pages_list}")
    return facts_str, mentions_list, mention_pages_list


def make_question(chosen_title, titles_q, found_entity_substr, used_titles):
    if chosen_title in titles_q and titles_q[chosen_title]:
        question = titles_q[chosen_title].format(found_entity_substr)
    else:
        if len(used_titles) == 1:
            question_template = QUESTION_TEMPLATES[0]
        else:
            question_template = random.choice(QUESTION_TEMPLATES)
        if found_entity_substr in chosen_title.lower() and question_template.endswith("of {}?"):
            question_template = question_template.split(" of {}?")[0] + "?"
            question = question_template.format(chosen_title)
        else:
            question = question_template.format(chosen_title, found_entity_substr)
    return question


def make_response(vars, prev_page_title, page_content, question):
    mentions_list = []
    mention_pages_list = []
    facts_str = ""
    if prev_page_title:
        paragraphs = find_paragraph(page_content, prev_page_title)
        facts_str, mentions_list, mention_pages_list = make_facts_str(paragraphs)
    logger.info(f"facts_str {facts_str} question {question}")
    response = f"{facts_str} {question}"
    response = response.strip()
    state_utils.save_to_shared_memory(vars, mentions=mentions_list)
    state_utils.save_to_shared_memory(vars, mention_pages=mention_pages_list)
    return response


def save_wiki_vars(vars, found_entity_substr_list, curr_pages, prev_title, prev_page_title, used_titles,
                   found_entity_types_list, new_page):
    state_utils.save_to_shared_memory(vars, found_entity_substr=found_entity_substr_list)
    state_utils.save_to_shared_memory(vars, curr_pages=curr_pages)
    state_utils.save_to_shared_memory(vars, prev_title=prev_title)
    state_utils.save_to_shared_memory(vars, prev_page_title=prev_page_title)
    state_utils.save_to_shared_memory(vars, used_titles=used_titles)
    state_utils.save_to_shared_memory(vars, found_entity_types=list(found_entity_types_list))
    state_utils.save_to_shared_memory(vars, new_page=new_page)


def start_talk_request(ngrams, vars):
    flag = False
    found_entity_substr_list, prev_title, prev_page_title, found_entity_types_list, used_titles, _, page_content_list, \
        main_pages_list, page = get_page_info(vars, "history")
    chosen_title, chosen_page_title = get_title_info(found_entity_substr_list[-1], found_entity_types_list[-1],
                                                     prev_title, used_titles, page_content_list[-1])
    user_uttr = state_utils.get_last_human_utterance(vars)
    bot_uttr = state_utils.get_last_bot_utterance(vars)
    user_dont_know = if_user_dont_know_topic(user_uttr, bot_uttr)
    if (chosen_title and found_entity_substr_list) or user_dont_know:
        flag = True
    logger.info(f"start_talk_request={flag}")
    return flag


def more_details_request(ngrams, vars):
    flag = False
    shared_memory = state_utils.get_shared_memory(vars)
    mentions_list = shared_memory.get("mentions", [])
    user_uttr = state_utils.get_last_human_utterance(vars)
    annotations = user_uttr["annotations"]
    bot_uttr = state_utils.get_last_bot_utterance(vars)
    bot_more_details = "more details" in bot_uttr["text"]
    user_more_details = re.findall(COMPILE_LETS_TALK, user_uttr["text"])
    isyes = is_yes(state_utils.get_last_human_utterance(vars))
    nounphrases = annotations.get("cobot_nounphrases", [])
    inters = set(nounphrases).intersection(set(mentions_list))
    if (user_more_details and inters) or (bot_more_details and isyes):
        flag = True
    logger.info(f"more_details_request={flag}")
    return flag


def factoid_q_request(ngrams, vars):
    flag = False
    user_uttr = state_utils.get_last_human_utterance(vars)
    bot_uttr = state_utils.get_last_bot_utterance(vars)
    user_more_details = re.findall(COMPILE_LETS_TALK, user_uttr["text"])
    user_annotations = user_uttr["annotations"]
    is_factoid = False
    factoid_cl = user_annotations.get("factoid_classification", {})
    if factoid_cl and factoid_cl["factoid"] > factoid_cl["conversational"]:
        is_factoid = True
    bot_text = bot_uttr["text"].lower()
    sentences = nltk.sent_tokenize(bot_text)
    if len(sentences) > 1:
        sentences = [sentence for sentence in sentences if not sentence.endswith("?")]
    bot_text = " ".join(sentences)
    nounphrases = user_annotations.get("cobot_nounphrases", [])
    found_nounphr = any([nounphrase in bot_text for nounphrase in nounphrases])
    logger.info(f"factoid_q_request, is_factoid {is_factoid} user_more_details {user_more_details} "
                f"nounphrases {nounphrases} bot_text {bot_text}")
    if is_factoid and not user_more_details and found_nounphr:
        flag = True
    logger.info(f"factoid_q_request={flag}")
    return flag


def tell_fact_request(ngrams, vars):
    flag = False
    user_uttr = state_utils.get_last_human_utterance(vars)
    found_entity_substr_list, prev_title, prev_page_title, found_entity_types_list, used_titles, _, page_content_list, \
        main_pages_list, page = get_page_info(vars)
    logger.info(f"request, found_entity_substr {found_entity_substr_list} prev_title {prev_title} "
                f"found_entity_types {found_entity_types_list} used_titles {used_titles}")
    chosen_title, chosen_page_title = get_title_info(found_entity_substr_list[-1], found_entity_types_list[-1],
                                                     prev_title,
                                                     used_titles, page_content_list[-1])
    logger.info(f"request, chosen_title {chosen_title} chosen_page_title {chosen_page_title}")
    isno = is_no(state_utils.get_last_human_utterance(vars))
    not_want = re.findall(COMPILE_NOT_WANT_TO_TALK_ABOUT_IT, user_uttr["text"])

    if chosen_title or (prev_title and not isno and not not_want):
        flag = True
    logger.info(f"tell_fact_request={flag}")
    return flag


def start_talk_response(vars):
    found_entity_substr_list, prev_title, _, found_entity_types_list, used_titles, curr_pages, page_content_list, \
        main_pages_list, page = get_page_info(vars, "history")
    response = f"Would you like to talk about {found_entity_substr_list[-1]}?"
    user_uttr = state_utils.get_last_human_utterance(vars)
    bot_uttr = state_utils.get_last_bot_utterance(vars)
    user_dont_know = if_user_dont_know_topic(user_uttr, bot_uttr)
    new_page = False
    if user_dont_know:
        topics = list(page_titles_by_entity_substr.keys())
        chosen_topic = random.choice(topics)
        response = f"Would you like to talk about {chosen_topic}?"
        curr_page = page_titles_by_entity_substr[chosen_topic]
        if curr_page:
            curr_pages.append(curr_page)
            new_page = True
        found_entity_substr_list.append(chosen_topic)
        found_entity_types_list.append([])
    save_wiki_vars(vars, found_entity_substr_list, curr_pages, "", "", [], found_entity_types_list, new_page)
    state_utils.set_confidence(vars, confidence=CONF_4)
    state_utils.set_can_continue(vars, continue_flag=common_constants.CAN_CONTINUE_PROMPT)
    return response


def more_details_response(vars):
    shared_memory = state_utils.get_shared_memory(vars)
    used_titles = shared_memory.get("used_titles", [])
    mentions_list = shared_memory.get("mentions", [])
    curr_pages = shared_memory.get("curr_pages", [])
    mention_pages_list = shared_memory.get("mention_pages", [])
    mentions_dict = {}
    for mention, mention_page in zip(mentions_list, mention_pages_list):
        mentions_dict[mention] = mention_page
    user_uttr = state_utils.get_last_human_utterance(vars)
    annotations = user_uttr["annotations"]
    nounphrases = annotations.get("cobot_nounphrases", [])
    inters = list(set(nounphrases).intersection(set(mentions_list)))
    found_entity_substr_list = []
    found_entity_substr = inters[0]
    found_entity_substr_list.append(found_entity_substr)
    found_entity_types = []
    new_page = False
    curr_page = mentions_dict[found_entity_substr]
    if curr_page:
        curr_pages.append(curr_page)
        new_page = True
    logger.info(f"more_details_response, found_entity_substr {found_entity_substr} curr_pages {curr_pages}")
    page_content, main_pages = get_page_content(curr_page)
    first_pars = page_content["first_par"]
    facts_str, new_mentions_list, new_mention_pages_list = make_facts_str(first_pars)
    titles_q, titles_we_use, all_titles = get_titles(found_entity_substr, found_entity_types, page_content)
    if not titles_we_use:
        titles_we_use = list(set(page_content.keys()).difference({"first_par"}))
    logger.info(f"all_titles {all_titles} titles_q {titles_q} titles_we_use {titles_we_use}")
    chosen_title, chosen_page_title = choose_title(vars, all_titles, titles_we_use, "", [])
    question = make_question(chosen_title, titles_q, found_entity_substr, [])
    response = f"{facts_str} {question}"
    response = response.strip()
    if chosen_title:
        used_titles.append(chosen_title)
    save_wiki_vars(vars, found_entity_substr_list, curr_pages, chosen_title, chosen_page_title, used_titles, [[]],
                   new_page)
    if response:
        state_utils.set_confidence(vars, confidence=CONF_1)
        state_utils.set_can_continue(vars, continue_flag=common_constants.MUST_CONTINUE)
    else:
        state_utils.set_confidence(vars, confidence=CONF_5)
        state_utils.set_can_continue(vars, continue_flag=common_constants.CAN_NOT_CONTINUE)
    return response


def factoid_q_response(vars):
    paragraphs = []
    shared_memory = state_utils.get_shared_memory(vars)
    prev_page_title = shared_memory.get("prev_page_title", "")
    mentions = shared_memory.get("mentions", [])
    mention_pages = shared_memory.get("mention_pages", [])
    curr_pages = shared_memory.get("curr_pages", [])
    new_page = shared_memory.get("new_page", False)
    if new_page and len(curr_pages) > 1:
        cur_page_content, cur_main_pages = get_page_content(curr_pages[-2])
        cur_paragraphs = find_paragraph(cur_page_content, prev_page_title)
        paragraphs += cur_paragraphs
        new_page_content, new_main_pages = get_page_content(curr_pages[-1])
        new_paragraphs = find_all_paragraphs(new_page_content, [])
        paragraphs += new_paragraphs
    else:
        cur_page_content, cur_main_pages = get_page_content(curr_pages[-1])
        cur_paragraphs = find_paragraph(cur_page_content, prev_page_title)
        paragraphs += cur_paragraphs
    logger.info(f"curr_pages {curr_pages} prev_page_title {prev_page_title}")

    mentions_dict = {}
    for mention, page in zip(mentions, mention_pages):
        mentions_dict[mention] = page
    user_uttr = state_utils.get_last_human_utterance(vars)
    user_annotations = user_uttr["annotations"]
    nounphrases = user_annotations.get("cobot_nounphrases", [])
    used_pages = []
    logger.info(f"nounphrases {nounphrases} mentions {mentions}")
    for nounphrase in nounphrases:
        for mention in mentions:
            if nounphrase in mention or mention in nounphrase:
                used_pages.append(mentions_dict[mention])
                break

    for page in used_pages:
        page_content, main_pages = get_page_content(page)
        paragraphs = find_all_paragraphs(page_content, paragraphs)

    clean_paragraphs = []
    for paragraph in paragraphs:
        clean_paragraph, _, _ = delete_hyperlinks(paragraph)
        clean_paragraphs.append(clean_paragraph)

    logger.info(f"clean_paragraphs {clean_paragraphs}")
    logger.info(f"factoid_q_response, used_pages {used_pages}")
    found_answer_sentence = ""
    try:
        res = requests.post(text_qa_url, json={"question_raw": [user_uttr["text"]], "top_facts": [clean_paragraphs]},
                            timeout=1.0)
        if res.status_code == 200:
            text_qa_output = res.json()[0]
            logger.info(f"text_qa_output {text_qa_output}")
            answer, conf, _, answer_sentence = text_qa_output
            if conf > ANSWER_CONF_THRES:
                found_answer_sentence = answer_sentence
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logger.exception(e)

    logger.info(f"found_answer_sentence {found_answer_sentence}")
    response = found_answer_sentence

    return response


def tell_fact_response(vars):
    shared_memory = state_utils.get_shared_memory(vars)
    found_entity_substr_list, prev_title, prev_page_title, found_entity_types_list, used_titles, curr_pages, \
        page_content_list, main_pages_list, new_page = get_page_info(vars)
    logger.info(f"tell_fact_response, found_entity_substr {found_entity_substr_list} prev_title {prev_title} "
                f"prev_page_title {prev_page_title} found_entity_types {found_entity_types_list} used_titles "
                f"{used_titles} curr_pages {curr_pages}")
    titles_q, titles_we_use, all_titles = get_titles(found_entity_substr_list[-1], found_entity_types_list[-1],
                                                     page_content_list[-1])
    logger.info(f"all_titles {all_titles} titles_q {titles_q} titles_we_use {titles_we_use}")
    chosen_title, chosen_page_title = choose_title(vars, all_titles, titles_we_use, prev_title, used_titles)
    logger.info(f"chosen_title {chosen_title} main_pages {main_pages_list}")
    if chosen_title:
        chosen_main_pages = main_pages_list[-1].get(chosen_page_title, [])
        new_page = False
        if chosen_main_pages:
            chosen_main_page = random.choice(chosen_main_pages)
            curr_pages.append(chosen_main_page)
            new_page = True
        used_titles.append(chosen_title)
        save_wiki_vars(vars, found_entity_substr_list, curr_pages, chosen_title, chosen_page_title, used_titles,
                       found_entity_types_list, new_page)
    else:
        save_wiki_vars(vars, [], [], "", "", [], [], False)

    question = make_question(chosen_title, titles_q, found_entity_substr_list[-1], used_titles)
    if new_page:
        if len(page_content_list) == 1:
            response = make_response(vars, prev_page_title, page_content_list[-1], question)
        else:
            response = make_response(vars, prev_page_title, page_content_list[-2], question)
    else:
        response = make_response(vars, prev_page_title, page_content_list[-1], question)
    started = shared_memory.get("start", False)
    if not started:
        state_utils.save_to_shared_memory(vars, start=True)
        state_utils.set_can_continue(vars, continue_flag=common_constants.CAN_CONTINUE_PROMPT)
    if response:
        state_utils.set_confidence(vars, confidence=CONF_1)
        state_utils.set_can_continue(vars, continue_flag=common_constants.MUST_CONTINUE)
    else:
        state_utils.set_confidence(vars, confidence=CONF_5)
        if started:
            state_utils.set_can_continue(vars, continue_flag=common_constants.CAN_NOT_CONTINUE)
    return response


def error_response(vars):
    state_utils.save_to_shared_memory(vars, start=False)
    save_wiki_vars(vars, [], [], "", "", [], [], False)
    state_utils.set_can_continue(vars, continue_flag=common_constants.CAN_NOT_CONTINUE)
    state_utils.set_confidence(vars, 0)
    return ""


simplified_dialog_flow = dialogflow_extention.DFEasyFilling(WikiState.USR_START)

simplified_dialog_flow.add_user_serial_transitions(
    WikiState.USR_START,
    {
        WikiState.SYS_FACTOID_Q: factoid_q_request,
        WikiState.SYS_TELL_FACT: tell_fact_request,
        WikiState.SYS_START_TALK: start_talk_request,
    },
)

simplified_dialog_flow.add_user_serial_transitions(
    WikiState.USR_MORE_DETAILED,
    {
        WikiState.SYS_TELL_FACT: tell_fact_request,
    },
)

simplified_dialog_flow.add_user_serial_transitions(
    WikiState.USR_START_TALK,
    {
        WikiState.SYS_FACTOID_Q: factoid_q_request,
        WikiState.SYS_TELL_FACT: tell_fact_request,
    },
)

simplified_dialog_flow.add_user_serial_transitions(
    WikiState.USR_TELL_FACT,
    {
        WikiState.SYS_FACTOID_Q: factoid_q_request,
        WikiState.SYS_MORE_DETAILED: more_details_request,
        WikiState.SYS_TELL_FACT: tell_fact_request,
    },
)

simplified_dialog_flow.add_system_transition(WikiState.SYS_TELL_FACT, WikiState.USR_TELL_FACT, tell_fact_response, )
simplified_dialog_flow.add_system_transition(WikiState.SYS_FACTOID_Q, WikiState.USR_FACTOID_Q, factoid_q_response, )
simplified_dialog_flow.add_system_transition(WikiState.SYS_MORE_DETAILED, WikiState.USR_MORE_DETAILED,
                                             more_details_response, )
simplified_dialog_flow.add_system_transition(WikiState.SYS_START_TALK, WikiState.USR_START_TALK, start_talk_response, )
simplified_dialog_flow.add_system_transition(WikiState.SYS_ERR, (scopes.MAIN, scopes.State.USR_ROOT), error_response, )

simplified_dialog_flow.set_error_successor(WikiState.USR_START, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.SYS_TELL_FACT, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.USR_TELL_FACT, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.SYS_START_TALK, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.USR_START_TALK, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.SYS_MORE_DETAILED, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.USR_MORE_DETAILED, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.SYS_FACTOID_Q, WikiState.SYS_ERR)
simplified_dialog_flow.set_error_successor(WikiState.USR_FACTOID_Q, WikiState.SYS_ERR)

dialogflow = simplified_dialog_flow.get_dialogflow()
