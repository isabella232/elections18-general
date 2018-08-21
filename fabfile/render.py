import app_config
import logging
import multiprocessing
import os
import re
import shutil
import simplejson as json

from datetime import datetime
from fabric.api import task
from joblib import Parallel, delayed
from models import models
from playhouse.shortcuts import model_to_dict

from . import utils

logging.basicConfig(format=app_config.LOG_FORMAT)
logger = logging.getLogger(__name__)
logger.setLevel(app_config.LOG_LEVEL)

NUM_CORES = multiprocessing.cpu_count() * 4

COMMON_SELECTIONS = [
    models.Result.first,
    models.Result.last,
    models.Result.lastupdated,
    models.Result.level,
    models.Result.officename,
    models.Result.party,
    models.Result.precinctsreporting,
    models.Result.precinctsreportingpct,
    models.Result.precinctstotal,
    models.Result.raceid,
    models.Result.statename,
    models.Result.statepostal,
    models.Result.votepct,
    models.Result.votecount,
    models.Result.winner
]

HOUSE_SELECTIONS = COMMON_SELECTIONS + [
    models.Result.incumbent,
    models.Result.runoff,
    models.Result.seatname,
    models.Result.seatnum,
    models.Result.meta
]

SENATE_SELECTIONS = COMMON_SELECTIONS + [
    models.Result.incumbent,
    models.Result.runoff,
    models.Result.meta
]

GOVERNOR_SELECTIONS = COMMON_SELECTIONS + [
    models.Result.incumbent,
    models.Result.meta
]

BALLOT_MEASURE_SELECTIONS = COMMON_SELECTIONS + [
    models.Result.officename,
    models.Result.seatname,
    models.Result.is_ballot_measure,
]

COUNTY_SELECTIONS = COMMON_SELECTIONS + [
    models.Result.reportingunitname,
    models.Result.fipscode
]

CALLS_SELECTIONS = [
    models.Call.accept_ap,
    models.Call.override_winner
]

RACE_META_SELECTIONS = [
    models.RaceMeta.poll_closing,
    models.RaceMeta.current_party,
    models.RaceMeta.full_poll_closing,
    models.RaceMeta.key_race
]

ACCEPTED_PARTIES = ['Dem', 'GOP', 'Yes', 'No']

SELECTIONS_LOOKUP = {
    'governor': GOVERNOR_SELECTIONS,
    'senate': SENATE_SELECTIONS,
    'house': HOUSE_SELECTIONS,
    'ballot_measures': BALLOT_MEASURE_SELECTIONS
}

OFFICENAME_LOOKUP = {
    'senate': 'U.S. Senate',
    'governor': 'Governor'
}


def _select_county_results(statepostal, office):
    results = models.Result.select().where(
        (models.Result.level == 'county') | (models.Result.level == 'state'),
        models.Result.officename == OFFICENAME_LOOKUP[office],
        models.Result.statepostal == statepostal
    )

    return results


def _select_governor_results():
    results = models.Result.select().where(
        models.Result.level == 'state',
        models.Result.officename == 'Governor'
    )

    return results


def _select_selected_house_results():
    results = models.Result.select().join(models.RaceMeta).where(
        models.Result.level == 'state',
        models.Result.officename == 'U.S. House',
        models.RaceMeta.key_race
    )

    return results


def _select_all_house_results():
    results = models.Result.select().join(models.RaceMeta).where(
        models.Result.level == 'state',
        models.Result.officename == 'U.S. House',
        ~(models.Result.is_special_election),
        models.RaceMeta.voting_member
    )

    return results


def _select_senate_results():
    results = models.Result.select().where(
        models.Result.level == 'state',
        models.Result.officename == 'U.S. Senate'
    )

    return results


def _select_ballot_measure_results():
    results = models.Result.select().where(
        models.Result.level == 'state',
        models.Result.is_ballot_measure
    )

    return results


@task
def render_top_level_numbers():
    # init with parties that already have seats

    # TO-DO: Here are the 2018 numbers, for use once the AP starts
    # running its 2018 tests in October
    # senate_bop = {
    #     'total_seats': 100,
    #     'majority': 51,
    #     'uncalled_races': 35,
    #     'last_updated': None,
    #     'Dem': {
    #         'seats': 23,
    #         'pickups': 0,
    #         'needed': 28
    #     },
    #     'GOP': {
    #         'seats': 42,
    #         'pickups': 0,
    #         'needed': 8
    #     },
    #     'Other': {
    #         'seats': 0,
    #         'pickups': 0,
    #         'needed': 51
    #     }
    # }

    senate_bop = {
        'total_seats': 100,
        'majority': 51,
        'uncalled_races': 34,
        'last_updated': None,
        'Dem': {
            'seats': 34,
            'pickups': 0,
            'needed': 17
        },
        'GOP': {
            'seats': 30,
            'pickups': 0,
            'needed': 21
        },
        'Other': {
            'seats': 2,
            'pickups': 0,
            'needed': 49
        }
    }

    house_bop = {
        'total_seats': 435,
        'majority': 218,
        'uncalled_races': 435,
        'last_updated': None,
        'Dem': {
            'seats': 0,
            'pickups': 0,
            'needed': 218
        },
        'GOP': {
            'seats': 0,
            'pickups': 0,
            'needed': 218
        },
        'Other': {
            'seats': 0,
            'pickups': 0,
            'needed': 218
        }
    }

    senate_results = _select_senate_results()
    house_results = _select_all_house_results()

    for result in senate_results:
        _calculate_bop(result, senate_bop)

    for result in house_results:
        _calculate_bop(result, house_bop)

    if senate_bop['last_updated'] > house_bop['last_updated'] or senate_bop['last_updated'] == house_bop['last_updated']:
        last_updated = senate_bop['last_updated']
    elif senate_bop['last_updated'] > house_bop['last_updated']:
        last_updated = house_bop['last_updated']
    else:
        last_updated = datetime.utcnow()

    data = {
        'senate_bop': senate_bop,
        'house_bop': house_bop,
        'last_updated': last_updated
    }

    _write_json_file(data, 'top-level-results.json')


@task
def render_county_results(office):
    states = models.Result.select(models.Result.statepostal).distinct()

    Parallel(n_jobs=NUM_CORES)(delayed(_render_county)(state.statepostal, office) for state in states)


def _render_county(statepostal, office):
    results = _select_county_results(statepostal, office)
    serialized_results = _serialize_by_key(results, COUNTY_SELECTIONS, 'fipscode', collate_other=True)

    # No need to render if the state doesn't have that type of race
    if serialized_results['results']:
        filename = '{0}-counties-{1}.json'.format(
            statepostal.lower(),
            office
        )
        _write_json_file(serialized_results, filename)


@task
def render_governor_results():
    results = _select_governor_results()

    serialized_results = _serialize_for_big_board(results, GOVERNOR_SELECTIONS)
    _write_json_file(serialized_results, 'governor-national.json')


@task
def render_house_results():
    results = _select_selected_house_results()

    serialized_results = _serialize_for_big_board(results, HOUSE_SELECTIONS)
    _write_json_file(serialized_results, 'house-national.json')


@task
def render_senate_results():
    results = _select_senate_results()

    serialized_results = _serialize_for_big_board(results, SENATE_SELECTIONS)
    _write_json_file(serialized_results, 'senate-national.json')


@task
def render_ballot_measure_results():
    results = _select_ballot_measure_results()

    serialized_results = _serialize_for_big_board(results, BALLOT_MEASURE_SELECTIONS)
    _write_json_file(serialized_results, 'ballot-measures-national.json')


@task
def render_state_results():
    states = models.Result.select(models.Result.statepostal).distinct()

    Parallel(n_jobs=NUM_CORES)(delayed(_render_state)(state.statepostal) for state in states)


def _render_state(statepostal):
    with models.db.execution_context() as ctx:
        senate = models.Result.select().where(
            models.Result.level == 'state',
            models.Result.officename == 'U.S. Senate',
            models.Result.statepostal == statepostal
        )
        house = models.Result.select().where(
            models.Result.level == 'state',
            models.Result.officename == 'U.S. House',
            models.Result.statepostal == statepostal,
            ~(models.Result.is_special_election)
        )
        governor = models.Result.select().where(
            models.Result.level == 'state',
            models.Result.officename == 'Governor',
            models.Result.statepostal == statepostal
        )
        ballot_measures = models.Result.select().where(
            models.Result.level == 'state',
            models.Result.is_ballot_measure,
            models.Result.statepostal == statepostal
        )

        state_results = {
            'results': {},
            'last_updated': None
        }
        queries = [senate, house, governor, ballot_measures]
        for query in queries:
            results_key = [k for k, v in locals().items() if v is query and k != 'query'][0]
            selectors = SELECTIONS_LOOKUP[results_key]
            state_results['results'][results_key] = _serialize_by_key(query, selectors, 'raceid', collate_other=True)
            if not state_results['last_updated'] or state_results['results'][results_key]['last_updated'] > state_results['last_updated']:
                state_results['last_updated'] = state_results['results'][results_key]['last_updated']

        filename = '{0}.json'.format(statepostal.lower())
        _write_json_file(state_results, filename)


uncallable_levels = ['county', 'township']
pickup_offices = ['U.S. House', 'U.S. Senate']


def _serialize_for_big_board(results, selections, key='raceid'):
    serialized_results = {
        'results': {}
    }

    for result in results:
        result_dict = model_to_dict(result, backrefs=True, only=selections)
        if result.level not in uncallable_levels:
            _set_meta(result, result_dict)
            if result.officename in pickup_offices:
                _set_pickup(result, result_dict)

        if not serialized_results['results'].get(result.meta[0].poll_closing):
            serialized_results['results'][result.meta[0].poll_closing] = {}

        if key == 'statepostal' and result.reportingunitname:
            m = re.search(r'\d$', result.reportingunitname)
            if m is not None:
                dict_key = '{0}-{1}'.format(result.statepostal, m.group())
            else:
                dict_key = result.statepostal
        else:
            dict_key = result_dict[key]

        time_bucket = serialized_results['results'][result.meta[0].poll_closing]
        if not time_bucket.get(dict_key):
            time_bucket[dict_key] = []

        time_bucket[dict_key].append(result_dict)

    serialized_results['last_updated'] = get_last_updated(serialized_results)
    return serialized_results


def _serialize_by_key(results, selections, key, collate_other=False):
    with models.db.execution_context():
        serialized_results = {
            'results': {}
        }

        for result in results:
            result_dict = model_to_dict(result, backrefs=True, only=selections)

            if result.level not in uncallable_levels:
                _set_meta(result, result_dict)
                if result.officename in pickup_offices:
                    _set_pickup(result, result_dict)

            # handle state results in the county files
            if key == 'fipscode' and result.level == 'state':
                dict_key = 'state'
            else:
                dict_key = result_dict[key]

            if not serialized_results['results'].get(dict_key):
                serialized_results['results'][dict_key] = []

            serialized_results['results'][dict_key].append(result_dict)

        serialized_results['last_updated'] = get_last_updated(serialized_results)

        if collate_other:
            serialized_results = collate_other_candidates(serialized_results)

        return serialized_results


def _set_meta(result, result_dict):
    meta = models.RaceMeta.get(models.RaceMeta.result_id == result.id)
    result_dict['meta'] = model_to_dict(meta, only=RACE_META_SELECTIONS)
    result_dict['npr_winner'] = result.is_npr_winner()


def _set_pickup(result, result_dict):
    result_dict['pickup'] = result.is_pickup()


def _calculate_bop(result, bop):
    party = result.party if result.party in ACCEPTED_PARTIES else 'Other'
    if result.is_npr_winner():
        bop[party]['seats'] += 1
        bop[party]['needed'] -= 1
        bop['uncalled_races'] -= 1

    if result.is_pickup():
        bop[party]['pickups'] += 1
        bop[result.meta[0].current_party]['pickups'] -= 1

    if not bop['last_updated'] or result.lastupdated > bop['last_updated']:
        bop['last_updated'] = result.lastupdated


def collate_other_candidates(serialized_results):
    # Create an "Other" candidate, to simplify front-end visuals,
    # and minimize filesize of JSON dumps

    # Here's a list of which candidates should and should not be turned
    # into "Other"s, based on the parties that are coming in:

    # Standard competitive races:
    # - D,R,I,I,... -> D,R,Oth
    # - D,R -> D,R
    # - D,I -> D,I
    # - R,I -> R,I

    # Less likely, relatively uncontested races:
    # - D,I,I -> D,I,Oth
    # - R,I,I -> R,I,Oth

    # Top-two general election (eg, California or Louisiana):
    # - D,D -> D,D
    # - R,R -> R,R

    # Uncontested races:
    # - D -> D
    # - R -> R
    # - I -> I

    # This won't happen for a top-of-ticket seat, but it's handled:
    # - I,I,I -> I,I,Oth
    # - I,I -> I,I

    TARGET_CANDIDATE_LIST_LENGTH = 2

    for key, val in serialized_results['results'].items():
        if isinstance(val, list):
            # Make sure that more prominent third-party candidates come first
            # But only order by votes if there are any votes in so far
            if val[0]['precinctsreporting'] > 0:
                val.sort(key=lambda c: c['votecount'], reverse=True)

            other_votecount = 0
            other_votepct = 0
            other_winner = False
            filtered = []

            accepted_party_count = len([c for c in val if c['party'] in ACCEPTED_PARTIES])
            third_party_slots = TARGET_CANDIDATE_LIST_LENGTH - accepted_party_count
            for result in val:
                if result['party'] in ACCEPTED_PARTIES:
                    filtered.append(result)
                elif third_party_slots > 0:
                    third_party_slots -= 1
                    filtered.append(result)
                else:
                    other_votecount += result['votecount']
                    other_votepct += result['votepct']
                    if result.get('npr_winner') is True:
                        other_winner = True

            # Don't create an "Other" if there are _only_ main-party
            # candidates in the race
            if len(val) > len(filtered):
                filtered.append({
                    'first': '',
                    'last': 'Other',
                    'votecount': other_votecount,
                    'votepct': other_votepct,
                    'npr_winner': other_winner
                })

            serialized_results['results'][key] = filtered

    return serialized_results


def get_last_updated(serialized_results):
    last_updated = None

    for key, val in serialized_results['results'].items():
        if isinstance(val, list):
            if val[0]['precinctsreporting'] > 0:
                for result in val:
                    if not last_updated or result['lastupdated'] > last_updated:
                        last_updated = result['lastupdated']

        elif isinstance(val, dict):
            for key, val in val.items():
                if val[0]['precinctsreporting'] > 0:
                    for result in val:
                        if not last_updated or result['lastupdated'] > last_updated:
                            last_updated = result['lastupdated']

    if not last_updated:
        last_updated = datetime.utcnow()

    return last_updated


def _write_json_file(serialized_results, filename):
    with open('{0}/{1}'.format(app_config.DATA_OUTPUT_FOLDER, filename), 'w') as f:
        json.dump(serialized_results, f, use_decimal=True, cls=utils.APDatetimeEncoder)


@task
def render_all():
    if os.path.isdir(app_config.DATA_OUTPUT_FOLDER):
        shutil.rmtree(app_config.DATA_OUTPUT_FOLDER)
    os.makedirs(app_config.DATA_OUTPUT_FOLDER)
    render_top_level_numbers()
    render_senate_results()
    render_governor_results()
    render_ballot_measure_results()
    render_house_results()
    render_state_results()
    render_county_results('senate')
    render_county_results('governor')
