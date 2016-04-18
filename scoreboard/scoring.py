"""Take game data and figure out scoring."""

import collections
import time
import traceback
import multiprocessing

import pylru

from . import model, constants

PLAYER_SCORE_CACHE = pylru.lrucache(1000, callback=model.set_player_score_data)
GLOBAL_SCORE_CACHE = pylru.lrucache(1000, callback=model.set_global_score)


def clean_up_active_streaks(streaks):
    """Remove single game "streaks"."""
    return {k: v for k, v in streaks.items() if len(v) > 1}


def is_valid_streak_addition(game, streak):
    """Check if the game is a valid addition to the streak."""
    # Extend active streak only if win started after previous game end
    if len(streak) == 0:
        return True
    return game['start'][:-1] > streak[-1]['end']


def load_player_scores(name):
    """Load the score dictionary of a player.

    This requires applying some transformations to the raw stored data.
    """
    if name in PLAYER_SCORE_CACHE:
        # print("loading %s from cache" % name)
        data = PLAYER_SCORE_CACHE[name]
    else:
        # print("loading %s from database" % name)
        data = model.get_player_score_data(name)

    if data:
        data['last_5_games'] = collections.deque(data['last_5_games'], 5)
    else:
        data = {'wins': [],
                'games': 0,
                'winrate': 0,
                'total_score': 0,
                'avg_score': 0,
                'last_5_games': collections.deque(
                    [], 5),
                'boring_games': 0,
                'boring_rate': 0,
                'god_wins': {k: 0 for k in constants.PLAYABLE_GODS},
                'race_wins': {k: 0 for k in constants.PLAYABLE_RACES},
                'role_wins': {k: 0 for k in constants.PLAYABLE_ROLES},
                'achievements': {},
                'last_active': None}
        # print("Created data for %s with %s elements in god_wins" % (name, len(data['god_wins'])))

    return data


def set_player_scores(name, data):
    """Save player scores to the LRU cache."""
    # print("storing %s in the cache" % name)
    PLAYER_SCORE_CACHE[name] = data


def load_global_scores(key, default=None):
    """Load a global score key from db/cache.

    If the key doesn't exist, return default.
    """
    if key in GLOBAL_SCORE_CACHE:
        val = GLOBAL_SCORE_CACHE[key]
    else:
        val = model.get_global_score(key)
    if default is not None and val is None:
        return default
    else:
        return val


def set_global_scores(key, data):
    """Save a global score key to cache.

    The cache is cleared out at the end of score_games.
    """
    GLOBAL_SCORE_CACHE[key] = data


def great_race(race, scores, achievements):
    """Check if the player has achieved great race for the given race.

    Returns True or False.

    Note: requires scores['race_wins'] to already have been updated with this
    games's result.
    """
    # Check the race has a greatrace achievement
    if race not in constants.RACE_TO_GREAT_RACE:
        return False
    achievement = constants.RACE_TO_GREAT_RACE[race]
    # Might have already achieved it
    if achievement in achievements:
        return True
    # Speed optimisation - check we actually have enough potential wins
    if scores['race_wins'][race] < len(constants.PLAYABLE_ROLES):
        return False
    # Check for completion
    roles_won = set(win['char'][2:]
                    for win in scores['wins'] if race == win['char'][:2])
    if not constants.PLAYABLE_ROLES - roles_won:
        achievements[achievement] = True


def great_role(role, scores, achievements):
    """Check if the player has achieved great role for the given role.

    Returns True or False.

    Note: requires scores['role_wins'] to already have been updated with this
    games's result.
    """
    # Check the role has a greatrole achievement
    if role not in constants.ROLE_TO_GREAT_ROLE:
        return False
    achievement = constants.ROLE_TO_GREAT_ROLE[role]
    # Might have already achieved it
    if achievement in achievements:
        return True
    # Speed optimisation - check we actually have enough potential wins
    if scores['role_wins'][role] < len(constants.PLAYABLE_RACES):
        return False
    # Check for completion
    races_won = set(win['char'][:2]
                    for win in scores['wins'] if role == win['char'][2:])
    if not constants.PLAYABLE_RACES - races_won:
        achievements[achievement] = True


def score_game_vs_global_highscores(log, fields):
    """Update global highscores with log's data.

    Returns True if a global record was updated.
    """
    result = False
    for field in fields:
        fieldval = log[field]
        highscores = load_global_scores(field + '_highscores', {})
        if fieldval not in highscores or log[
                'sc'] > highscores[fieldval]['sc']:
            highscores[fieldval] = log
            set_global_scores(field + '_highscores', highscores)
            result = True
    return result


def score_game_vs_misc_stats(log):
    """Compare a game log with various misc global stats.

    Note: Currently assumes the game was won.
    """
    dur = log['dur']
    turns = log['turn']

    min_dur = load_global_scores('min_dur', [])
    if not min_dur or len(min_dur) < 5:
        min_dur.append(log)
    else:
        if dur < min(i['dur'] for i in min_dur):
            min_dur.append(log)
            min_dur = sorted(min_dur, key=lambda i: i['dur'])[:5]
    set_global_scores('min_dur', min_dur)

    min_turn = load_global_scores('min_turn', [])
    if not min_turn or len(min_turn) < 5:
        min_turn.append(log)
    else:
        if turns < min(i['turn'] for i in min_turn):
            min_turn.append(log)
            min_turn = sorted(min_turn, key=lambda i: i['turn'])[:5]
    set_global_scores('min_turn', min_turn)


def score_game_vs_streaks(log, won):
    """Extend active streaks if a game was won and finalise streak stats."""
    active_streaks = load_global_scores('active_streaks', {})
    name = log['name']
    if won:
        # Extend or start a streak
        if name in active_streaks:
            if is_valid_streak_addition(log, active_streaks[name]):
                active_streaks[name].append(log)
        else:
            active_streaks[name] = [log]
    else:
        # If the player was on a 2+ game streak, record it
        if len(active_streaks.get(name, [])) > 1:
            completed_streaks = load_global_scores('completed_streaks', [])
            streak = active_streaks[name]
            completed_streaks.append({'player': name,
                                      'wins': streak,
                                      'streak_breaker': log,
                                      'start': streak[0]['start'],
                                      'end': streak[-1]['end']})
            set_global_scores('completed_streaks', completed_streaks)
        if name in active_streaks:
            del active_streaks[name]
        else:
            # No need to adjust active_streaks
            return
    set_global_scores('active_streaks', active_streaks)


def score_game(game):
    """Score a single game."""
    gid = game[0]
    log = game[1]

    # Log vars
    name = log['name']
    god = log['god']
    score = log['sc']
    race = log['rc']
    role = log['bg']
    won = log['ktyp'] == 'winning'

    # Player vars
    scores = load_player_scores(name)
    achievements = scores['achievements']
    wins = len(scores['wins'])

    # Increment games
    scores['games'] += 1
    scores['last_active'] = log['end']

    # Increment wins
    if won:
        scores['wins'].append(log)

        # Adjust fastest_realtime win
        if 'fastest_realtime' not in scores or log['dur'] < scores[
                'fastest_realtime']['dur']:
            scores['fastest_realtime'] = log

        # Adjust fastest_turncount win
        if 'fastest_turncount' not in scores or log['turn'] < scores[
                'fastest_turncount']['turn']:
            scores['fastest_turncount'] = log

        # Increment god_wins and check polytheist
        if god in scores['god_wins']:
            scores['god_wins'][god] += 1
            if scores['god_wins'][god] == 1 and \
                    not constants.PLAYABLE_GODS - {g for g, w in scores['god_wins'].items() if w > 0}:
                achievements['polytheist'] = True
        else:
            scores['god_wins'][god] += 1

        # Increment race_wins and check greatplayer
        if race not in scores['race_wins']:
            scores['race_wins'][race] = 1
            if not constants.PLAYABLE_RACES - scores['race_wins'].keys():
                achievements['greatplayer'] = True
        else:
            scores['race_wins'][race] += 1

        # Increment role_wins and check greaterplayer
        if role not in scores['role_wins']:
            scores['role_wins'][role] = 1
            if not constants.PLAYABLE_ROLES - scores['role_wins'].keys() \
                    and 'greatplayer' in achievements:
                achievements['greaterplayer'] = True
        else:
            scores['role_wins'][role] += 1

        # Adjust avg_win stats
        # Older logfiles didn't have these fields, so skip those games
        if 'ac' in log:
            if 'avg_win_ac' not in scores:
                scores['avg_win_ac'] = log['ac']
            else:
                scores['avg_win_ac'] += (
                    log['ac'] - scores['avg_win_ac']) / wins
            if 'avg_win_ev' not in scores:
                scores['avg_win_ev'] = log['ev']
            else:
                scores['avg_win_ev'] += (
                    log['ev'] - scores['avg_win_ev']) / wins
            if 'avg_win_sh' not in scores:
                scores['avg_win_sh'] = log['sh']
            else:
                scores['avg_win_sh'] += (
                    log['sh'] - scores['avg_win_sh']) / wins

        # Adjust win-based achievements
        if wins >= 10:
            achievements['goodplayer'] = True
        if wins >= 100:
            achievements['centuryplayer'] = True

        # Check for great race completion
        if great_race(race, scores, achievements):
            achievements[constants.RACE_TO_GREAT_RACE[race]] = True

        # Check for great role completion
        if great_role(role, scores, achievements):
            achievements[constants.ROLE_TO_GREAT_ROLE[role]] = True

        # Older logfiles don't have these fields, so skip those games
        if 'potionsused' in log and log['potionsused'] == 0 and log[
                'scrollsused'] == 0:
            if 'no_potion_or_scroll_win' not in achievements:
                achievements['no_potion_or_scroll_win'] = 1
            else:
                achievements['no_potion_or_scroll_win'] += 1

        if 'zigdeepest' in log and log['zigdeepest'] == '27':
            if 'cleared_zig' not in achievements:
                achievements['cleared_zig'] = 1
            else:
                achievements['cleared_zig'] += 1

        # Compare win against misc global stats
        score_game_vs_misc_stats(log)

    else:  # !won
        # Increment boring_games
        if log['ktyp'] in ('leaving', 'quitting'):
            scores['boring_games'] += 1

    # Update other player stats
    if 'highscore' not in scores or score > scores['highscore']['sc']:
        scores['highscore'] = log
    scores['winrate'] = wins / scores['games']
    scores['total_score'] += score
    scores['avg_score'] = scores['total_score'] / scores['games']
    scores['last_5_games'].append(log)
    scores['boring_rate'] = scores['boring_games'] / scores['games']

    # Check global highscore records
    if score_game_vs_global_highscores(log, ['char']):
        # Only check rc and bg records if char record was broken
        score_game_vs_global_highscores(log, ['rc', 'bg'])
    score_game_vs_global_highscores(log, ['god'])

    # Check streaks
    score_game_vs_streaks(log, won)

    # Finalise the changes to stats
    set_player_scores(name, scores)
    model.mark_game_scored(gid)


def score_games():
    """Update scores with all game's data."""
    print("Scoring all games...")
    start = time.time()
    scored = 0

    p = multiprocessing.Pool(processes=multiprocessing.cpu_count())
    jobs = []
    for game in model.games(scored=False):
        jobs.append(p.apply_async(score_game, (game,)))

    for async_job in jobs:
        async_job.wait()

        # Periodically print our progress
        scored += 1
        if scored % 10000 == 0:
            print(scored)

    # clean up single-game "streaks"
    set_global_scores(
        'active_streaks',
        clean_up_active_streaks(load_global_scores('active_streaks')))

    # Now we have to write out everything remaining in the cache
    for name, scores in PLAYER_SCORE_CACHE.items():
        model.set_player_score_data(name, scores)
    for key, data in GLOBAL_SCORE_CACHE.items():
        model.set_global_score(key, data)
    end = time.time()
    print("Scored %s games in %s secs" % (scored, round(end - start, 2)))


if __name__ == "__main__":
    score_games()
