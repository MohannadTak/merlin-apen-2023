import argparse
import concurrent.futures
from copy import deepcopy
import inspect
import itertools
import os
from multiprocessing import cpu_count
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import pandas as pd
from citylearn.data import DataSet
from citylearn.utilities import read_json, write_json
from database import SQLiteDatabase

TIMESTAMPS = pd.DataFrame(pd.date_range('2016-08-01 00:00:00','2017-07-31 23:00:00',freq='H'),columns=['timestamp'])
TIMESTAMPS['time_step'] = TIMESTAMPS.index
ROOT_DIRECTORY = os.path.join(*Path(os.path.dirname(__file__)).absolute().parts[0:-1])
DATA_DIRECTORY = os.path.join(ROOT_DIRECTORY,'data')

def set_result_summary(experiment,detailed=False):
    if detailed:
        set_detailed_summary(experiment)
    else:
        set_brief_summary(experiment)

def set_detailed_summary(experiment):
    kwargs = preliminary_setup()
    result_directory = kwargs['result_directory']
    database_directory = kwargs['database_directory']
    misc_directory = kwargs['misc_directory']
    database_filepath = os.path.join(database_directory,f'{experiment}.db')
    grid_filepath = os.path.join(misc_directory,f'{experiment}_grid.csv')
    grid = pd.read_csv(grid_filepath)
    grid = grid.rename(columns={'group':'simulation_group'})
    grid_column_types = []

    for c in grid.columns:
        if pd.api.types.is_numeric_dtype(grid[c]):
            grid_column_types.append(f'{c} REAL')
        else:
            grid_column_types.append(f'{c} TEXT')
    filenames = sorted([
        f for f in os.listdir(result_directory) 
        if f.endswith('pkl') and experiment in f and 'agent' not in f
    ])

    if os.path.isfile(database_filepath):
        os.remove(database_filepath)
    else:
        pass
    
    db = SQLiteDatabase(database_filepath)
    query = f"""
    DROP TABLE IF EXISTS grid;
    CREATE TABLE grid (
        {','.join(grid_column_types)},
        PRIMARY KEY (simulation_id)
    );
    DROP TABLE IF EXISTS detailed_summary;
    CREATE TABLE detailed_summary (
        timestamp TEXT,
        date TEXT,
        time_step INTEGER,
        simulation_id TEXT,
        episode INTEGER,
        building_id INTEGER,
        building_name TEXT,
        net_electricity_consumption REAL,
        net_electricity_consumption_emission REAL,
        net_electricity_consumption_price REAL,
        net_electricity_consumption_without_storage REAL,
        net_electricity_consumption_emission_without_storage REAL,
        net_electricity_consumption_price_without_storage REAL,
        net_electricity_consumption_without_storage_and_pv REAL,
        electrical_storage_soc REAL,
        electrical_storage_electricity_consumption REAL,
        action REAL,
        reward REAL,
        PRIMARY KEY (simulation_id, episode, time_step, building_id),
        FOREIGN KEY (simulation_id) REFERENCES grid (simulation_id)
            ON UPDATE CASCADE
            ON DELETE NO ACTION
    );
    """
    _ = db.query(query)
    db.insert('grid', grid.columns, grid.values)
    actions = get_actions_from_log(experiment)

    for i, f in enumerate(filenames):
        print(f'Reading {i + 1}/{len(filenames)}')
        episode = int(f.split('.')[0].split('_')[-1])
        simulation_id = '_'.join(f.split('_')[0:-2])
            
        with (open(os.path.join(result_directory,f), 'rb')) as openfile:
            env = pickle.load(openfile)

        rewards = pd.DataFrame(env.rewards)
        
        for j, b in enumerate(env.buildings):
            temp_data = pd.DataFrame({
                'net_electricity_consumption':b.net_electricity_consumption,
                'net_electricity_consumption_emission':b.net_electricity_consumption_emission,
                'net_electricity_consumption_price':b.net_electricity_consumption_price,
                'net_electricity_consumption_without_storage':b.net_electricity_consumption_without_storage,
                'net_electricity_consumption_emission_without_storage':b.net_electricity_consumption_without_storage_emission,
                'net_electricity_consumption_price_without_storage':b.net_electricity_consumption_without_storage_price,
                'net_electricity_consumption_without_storage_and_pv':b.net_electricity_consumption_without_storage_and_pv,
                'electrical_storage_soc':b.electrical_storage.soc,
                'electrical_storage_electricity_consumption':b.electrical_storage.electricity_consumption,
                'reward':rewards[j].tolist(),
            })
            temp_data['time_step'] = temp_data.index
            temp_data['simulation_id'] = simulation_id
            temp_data['episode'] = episode
            temp_data['building_id'] = j
            temp_data['building_name'] = b.name
            temp_data = temp_data.merge(TIMESTAMPS, on='time_step', how='left')
            temp_data['date'] = temp_data['timestamp'].dt.strftime('%Y-%m-%d')
            temp_data['timestamp'] = temp_data['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
            temp_data = temp_data.merge(actions, on=['time_step', 'episode', 'simulation_id', 'building_id'], how='left')
            db.insert('detailed_summary', temp_data.columns, temp_data.values)

    query = """
    CREATE INDEX IF NOT EXISTS detailed_summary_timestamp ON detailed_summary (timestamp);
    CREATE INDEX IF NOT EXISTS detailed_summary_date ON detailed_summary (date);
    CREATE INDEX IF NOT EXISTS detailed_summary_time_step ON detailed_summary (time_step);
    CREATE INDEX IF NOT EXISTS detailed_summary_simulation_id ON detailed_summary (simulation_id);
    CREATE INDEX IF NOT EXISTS detailed_summary_episode ON detailed_summary (episode);
    CREATE INDEX IF NOT EXISTS detailed_summary_building_id ON detailed_summary (building_id);
    CREATE INDEX IF NOT EXISTS detailed_summary_building_name ON detailed_summary (building_name);
    """
    db.query(query)

def get_actions_from_log(experiment):
    kwargs = preliminary_setup()
    directory = kwargs['log_directory']
    files = [f for f in os.listdir(directory) if experiment in f]
    data_list = []

    for f in files:
        with open(os.path.join(directory, f), 'r') as d:
            data = d.read()
        
        data = data.replace('\n',' ')
        data = data.replace('\r',' ')
        data = data.split(': Time step: ')[1:]
        data = [{
            'time_step': int(d.split('/')[0]),
            'episode': int(d.split('/')[1].split(' ')[-1]),
            'action': d.split('Actions: ')[1].split(', Rewards')[0].replace('[', '').replace(']', '').split(', ')
        } for d in data]
        buildings = [i for i in range(len(data[0]['action']))]*len(data)
        data = pd.DataFrame(data)
        data['simulation_id'] = f.strip('simulation_').split('.')[0]
        data = data.explode(column='action')
        data['action'] = data['action'].astype(float)
        data['building_id'] = buildings
        data_list.append(data)

    data = pd.concat(data_list, ignore_index=True, sort=False)
    
    return data

def set_brief_summary(experiment):
    kwargs = preliminary_setup()
    result_directory = kwargs['result_directory']
    summary_directory = kwargs['summary_directory']
    filenames = [
        f for f in os.listdir(result_directory) 
        if f.endswith('pkl') and experiment in f and 'agent' not in f
    ]
    records = []

    for i, f in enumerate(filenames):
        print(f'Reading {i + 1}/{len(filenames)}')
        episode = int(f.split('.')[0].split('_')[-1])
        simulation_id = '_'.join(f.split('_')[0:-2])
            
        with (open(os.path.join(result_directory,f), 'rb')) as openfile:
            env = pickle.load(openfile)

        rewards = pd.DataFrame(env.rewards)
        
        for j, b in enumerate(env.buildings):
            records.append({
                'experiment':experiment,
                'simulation_id':simulation_id,
                'episode':episode,
                'building_id':j,
                'building_name':b.name,
                'reward_sum':rewards[j].sum(),
                'reward_mean':rewards[j].mean(),
                'net_electricity_consumption_sum':sum(b.net_electricity_consumption),
                'net_electricity_consumption_emission_sum':sum(b.net_electricity_consumption_emission),
                'net_electricity_consumption_price_sum':sum(b.net_electricity_consumption_price),
                'net_electricity_consumption_without_storage_sum':sum(b.net_electricity_consumption_without_storage),
                'net_electricity_consumption_emission_without_storage_sum':sum(b.net_electricity_consumption_without_storage_emission),
                'net_electricity_consumption_price_without_storage_sum':sum(b.net_electricity_consumption_without_storage_price),
            })
    
    data = pd.DataFrame(records)
    filepath = os.path.join(summary_directory,f'{experiment}_brief.csv')
    data.to_csv(filepath,index=False)

def run(experiment, virtual_environment_path=None, windows_system=None):
    kwargs = preliminary_setup()
    work_order_directory = kwargs['work_order_directory']
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')

    if virtual_environment_path is not None:    
        if windows_system:
            virtual_environment_command = f'"{os.path.join(virtual_environment_path, "Scripts", "Activate.ps1")}"'
        else:
            virtual_environment_command = f'source "{os.path.join(virtual_environment_path, "bin", "activate")}"'
    else:
        virtual_environment_command = 'echo "No virtual environment"'

    with open(work_order_filepath,mode='r') as f:
        args = f.read()
    
    args = args.strip('\n').split('\n')
    args = [f'{virtual_environment_command} && {a}' for a in args]
    settings = get_settings()
    max_workers = settings['max_workers'] if settings.get('max_workers',None) is not None else cpu_count()
    
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        print(f'Will use {max_workers} workers for job.')
        print(f'Pooling {len(args)} jobs to run in parallel...')
        results = [executor.submit(subprocess.run,**{'args':a, 'shell':True}) for a in args]
            
        for future in concurrent.futures.as_completed(results):
            try:
                print(future.result())
            except Exception as e:
                print(e)

def set_deployment_strategy_work_order(experiment):
    kwargs = preliminary_setup()
    schema = kwargs['schema']
    schema_directory = kwargs['schema_directory']
    src_directory = kwargs['src_directory']
    misc_directory = kwargs['misc_directory']
    work_order_directory = kwargs['work_order_directory']
    tacc_directory = kwargs['tacc_directory']
    agent_directory = kwargs['agent_directory']
    settings = get_settings()

    # 1_0 - all buildings, find optimal policy on full year data
    # 1_1 - all buildings, find optimal policy on full year data and test on half year for comparison with 3_1
    # 2_0 - transfer policy between buildings for 1 episode & evaluate determinitically
    # 3_0 - all buildings, find optimal policy on half year data
    # 3_1 - test on remaining timesteps 1 episode & evaluate determinitically
    # 3_2 - transfer policy between buildings for 1 episode & evaluate determinitically
    simulation_start_time_step = {
        'deployment_strategy_1_0':settings["train_start_time_step"], 
        'deployment_strategy_1_1':settings["test_start_time_step"], 
        'deployment_strategy_2_0':settings["train_start_time_step"], 
        'deployment_strategy_3_0':settings["train_start_time_step"], 
        'deployment_strategy_3_1':settings["test_start_time_step"],
        'deployment_strategy_3_2':settings["test_start_time_step"],
    }
    simulation_end_time_step = {
        'deployment_strategy_1_0':settings["test_end_time_step"], 
        'deployment_strategy_1_1':settings["test_end_time_step"], 
        'deployment_strategy_2_0':settings["test_end_time_step"], 
        'deployment_strategy_3_0':settings["train_end_time_step"], 
        'deployment_strategy_3_1':settings["test_end_time_step"],
        'deployment_strategy_3_2':settings["test_end_time_step"],
    }
    episodes = {
        'deployment_strategy_1_0':schema['episodes'],
        'deployment_strategy_1_1':1,
        'deployment_strategy_2_0':1,
        'deployment_strategy_3_0':schema['episodes'],
        'deployment_strategy_3_1':1,
        'deployment_strategy_3_2':1,
    }
    deterministic_start_time_step = {
        'deployment_strategy_1_0':(simulation_end_time_step['deployment_strategy_1_0'] + 1)*(episodes['deployment_strategy_1_0'] - 1),
        'deployment_strategy_1_1':0,
        'deployment_strategy_2_0':0,
        'deployment_strategy_3_0':(simulation_end_time_step['deployment_strategy_3_0'] + 1)*(episodes['deployment_strategy_3_0'] - 1),
        'deployment_strategy_3_1':0,
        'deployment_strategy_3_2':0,
    }
    save_episode_agent = {
        'deployment_strategy_1_0':episodes['deployment_strategy_1_0'] - 1,
        'deployment_strategy_1_1':None,
        'deployment_strategy_2_0':None,
        'deployment_strategy_3_0':episodes['deployment_strategy_3_0'] - 1,
        'deployment_strategy_3_1':None,
        'deployment_strategy_3_2':None,
    }
    agent_filepath_sources = {
        'deployment_strategy_1_0':None,
        'deployment_strategy_1_1':'deployment_strategy_1_0',
        'deployment_strategy_2_0':'deployment_strategy_1_0',
        'deployment_strategy_3_0':None,
        'deployment_strategy_3_1':'deployment_strategy_3_0',
        'deployment_strategy_3_2':'deployment_strategy_3_0',
    }
    transfer_agents = {
        'deployment_strategy_1_0':False,
        'deployment_strategy_1_1':False,
        'deployment_strategy_2_0':True,
        'deployment_strategy_3_0':False,
        'deployment_strategy_3_1':False,
        'deployment_strategy_3_2':True,
    }
    deterministic_agents = {
        'deployment_strategy_1_0':False,
        'deployment_strategy_1_1':True,
        'deployment_strategy_2_0':True,
        'deployment_strategy_3_0':False,
        'deployment_strategy_3_1':True,
        'deployment_strategy_3_2':True,
    }

    # set optimal schema
    schema = get_optimal_schema(schema)
    schema['simulation_start_time_step'] = simulation_start_time_step[experiment]
    schema['simulation_end_time_step'] = simulation_end_time_step[experiment]
    schema['agent']['attributes']['deterministic_start_time_step'] = deterministic_start_time_step[experiment]
    schema['episodes'] = episodes[experiment]

    agent_episode = save_episode_agent[experiment]
    agent_filepath_source = agent_filepath_sources[experiment]
    transfer = transfer_agents[experiment]
    deterministic = deterministic_agents[experiment]
    
    # set grid
 
    if transfer:
        grid = pd.DataFrame({'building':list(schema['buildings'].keys())})
        grid['group'] = grid.index
        grid_list = []
        
        for s in settings['seeds']:
            grid['seed'] = s
            grid_list.append(grid.copy())

        grid = pd.concat(grid_list, ignore_index=True, sort=False)
    
    else:
        grid = pd.DataFrame({'seed':settings['seeds']})
        grid['group'] = 0

    grid['simulation_id'] = grid.reset_index().index.map(lambda x: f'{experiment}_{x}')
    grid.to_csv(os.path.join(misc_directory,f'{experiment}_grid.csv'),index=False)

    # design work order
    work_order = []

    for i, params in enumerate(grid.to_dict('records')):
        schema['agent']['attributes'] = {
            **schema['agent']['attributes'],
            'seed':int(params['seed'])
        }
        schema_filepath = os.path.join(schema_directory,f'{params["simulation_id"]}.json')

        if transfer:
            temp_schema = deepcopy(schema)

            for b in temp_schema['buildings']:
                temp_schema['buildings'][b] = temp_schema['buildings'][params['building']]

            write_json(schema_filepath, temp_schema)

        else:
            write_json(schema_filepath, schema)

        command = f'python "{os.path.join(src_directory,"simulate.py")}" "{schema_filepath}" {params["simulation_id"]}'
        command += f' --save_episode_agent {agent_episode}' if agent_episode is not None else ''
        agent_filepath = os.path.join(
            agent_directory, 
            f'{agent_filepath_sources[experiment]}_{params["seed"]}_agent_episode_{int(save_episode_agent[agent_filepath_source] - 1)}.pkl'
        ) if agent_filepath_source is not None else None
        command += f' --agent_filepath "{agent_filepath}"' if agent_filepath is not None else ''
        command += f' --deterministic' if deterministic is not None else ''
        work_order.append(command)

    # write work order and tacc job
    tacc_job = get_tacc_job(experiment, nodes=len(work_order) + 1)
    work_order.append('')
    work_order = '\n'.join(work_order)
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')
    tacc_job_filepath = os.path.join(tacc_directory,f'{experiment}.sh')

    for d, p in zip([work_order,tacc_job],[work_order_filepath,tacc_job_filepath]):
        with open(p,'w') as f:
            f.write(d)

def get_optimal_schema(schema):
    settings = get_settings()

    # optimal reward
    schema['reward_function'] = {
        'type': settings['experiments']['reward_design']['optimal']['type'],
        'attributes': {
            'electricity_price_weight': float(settings['experiments']['reward_design']['optimal']['weight']),
            'carbon_emission_weight': float(1.0 - settings['experiments']['reward_design']['optimal']['weight']),
            'electricity_price_exponent': float(settings['experiments']['reward_design']['optimal']['exponent']),
            'carbon_emission_exponent': float(settings['experiments']['reward_design']['optimal']['exponent']),
        } 
    }

    # optimal agent
    schema['agent']['type'] = 'agent.SAC' + settings['experiments']['rbc_validation']['optimal'].split('.')[-1]
    schema['agent']['attributes'] = {
        **schema['agent']['attributes'],
        **settings['experiments']['hyperparameter_design']['optimal']
    }

    return schema

def set_rbc_reference_work_order(experiment):
    kwargs = preliminary_setup()
    schema = kwargs['schema']
    schema_directory = kwargs['schema_directory']
    src_directory = kwargs['src_directory']
    misc_directory = kwargs['misc_directory']
    work_order_directory = kwargs['work_order_directory']
    tacc_directory = kwargs['tacc_directory']
    settings = get_settings()

    start_timestamps = {
        'rbc_reference_1':settings["train_start_time_step"], 
        'rbc_reference_3': settings["test_start_time_step"]
    }
    schema['simulation_end_time_step'] = settings["test_end_time_step"]
    schema['simulation_start_time_step'] = start_timestamps[experiment]
    schema['episodes'] = 1
    grid = pd.DataFrame({'type':[settings['experiments']['rbc_validation']['optimal']]})
    grid['simulation_group'] = grid.index
    grid['simulation_id'] = grid.reset_index().index.map(lambda x: f'{experiment}_{x}')
    grid.to_csv(os.path.join(misc_directory,f'{experiment}_grid.csv'),index=False)
    work_order = []

    # update agent
    for i, params in enumerate(grid.to_records(index=False)):
        schema['agent'] = {
            'type': params['type'],
        }
        schema_filepath = os.path.join(schema_directory,f'{params["simulation_id"]}.json')
        write_json(schema_filepath, schema)
        work_order.append(f'python "{os.path.join(src_directory,"simulate.py")}" "{schema_filepath}" {params["simulation_id"]}')

    # write work order

    tacc_job = get_tacc_job(experiment)
    tacc_job_filepath = os.path.join(tacc_directory,f'{experiment}.sh')
    work_order.append('')
    work_order = '\n'.join(work_order)
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')

    for d, p in zip([work_order,tacc_job],[work_order_filepath,tacc_job_filepath]):
        with open(p,'w') as f:
            f.write(d)

def set_rbc_validation_work_order(experiment):
    kwargs = preliminary_setup()
    schema = kwargs['schema']
    schema_directory = kwargs['schema_directory']
    src_directory = kwargs['src_directory']
    misc_directory = kwargs['misc_directory']
    work_order_directory = kwargs['work_order_directory']
    tacc_directory = kwargs['tacc_directory']
    settings = get_settings()

    # update general settings
    schema['simulation_end_time_step'] = settings["test_end_time_step"]
    schema['episodes'] = 1
    grid = pd.DataFrame({'type':settings['experiments'][experiment]['type']}) 
    grid['simulation_id'] = grid.reset_index().index.map(lambda x: f'{experiment}_{x}')
    grid.to_csv(os.path.join(misc_directory,f'{experiment}_grid.csv'),index=False)
    work_order = []

    # update agent
    for i, params in enumerate(grid.to_records(index=False)):
        schema['agent'] = {
            'type': params['type'],
        }
        schema_filepath = os.path.join(schema_directory,f'{params["simulation_id"]}.json')
        write_json(schema_filepath, schema)
        work_order.append(f'python "{os.path.join(src_directory,"simulate.py")}" "{schema_filepath}" {params["simulation_id"]}')

    # write work order
    tacc_job = get_tacc_job(experiment)
    tacc_job_filepath = os.path.join(tacc_directory,f'{experiment}.sh')
    work_order.append('')
    work_order = '\n'.join(work_order)
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')

    for d, p in zip([work_order,tacc_job],[work_order_filepath,tacc_job_filepath]):
        with open(p,'w') as f:
            f.write(d)

def set_hyperparameter_design_work_order(experiment):
    kwargs = preliminary_setup()
    schema = kwargs['schema']
    schema_directory = kwargs['schema_directory']
    src_directory = kwargs['src_directory']
    misc_directory = kwargs['misc_directory']
    work_order_directory = kwargs['work_order_directory']
    tacc_directory = kwargs['tacc_directory']
    settings = get_settings()

    # set active buildings
    train_buildings = settings['design_buildings']

    for building in schema['buildings']:
        schema['buildings'][building]['include'] = True if int(building.split('_')[-1]) in train_buildings else False

    simulation_end_time_step = {
        'hyperparameter_design_1': settings['test_end_time_step'],
        'hyperparameter_design_3': settings['train_end_time_step'],
    }

    schema['simulation_end_time_step'] = simulation_end_time_step[experiment]
    schema['agent']['attributes']['deterministic_start_time_step'] = (schema['simulation_end_time_step'] + 1)*(schema['episodes'] - 1)

    # hyperparameter definition
    hyperparameter_grid = settings['experiments']['hyperparameter_design']['grid']
    param_names = list(hyperparameter_grid.keys())
    param_values = list(hyperparameter_grid.values())
    param_values_grid = list(itertools.product(*param_values))
    grid = pd.DataFrame(param_values_grid,columns=param_names)
    grid['group'] = grid.index
    grid_list = []

    for seed in settings['seeds']:
        grid['seed'] = seed
        grid_list.append(grid.copy())

    grid = pd.concat(grid_list,ignore_index=True)
    grid = grid.sort_values(['seed'])
    grid['buildings'] = str(train_buildings)
    grid['simulation_id'] = grid.reset_index().index.map(lambda x: f'{experiment}_{x}')
    grid.to_csv(os.path.join(misc_directory,f'{experiment}_grid.csv'),index=False)

    # design work order
    work_order = []

    for i, params in enumerate(grid.to_dict('records')):
        params['seed'] = int(params['seed'])
        schema['agent']['attributes'] = {
            **schema['agent']['attributes'],
            **params
        }
        schema_filepath = os.path.join(schema_directory,f'{params["simulation_id"]}.json')
        write_json(schema_filepath, schema)
        work_order.append(f'python "{os.path.join(src_directory,"simulate.py")}" "{schema_filepath}" {params["simulation_id"]}')

    # write work order and tacc job
    work_order.append('')
    work_order = '\n'.join(work_order)
    tacc_job = get_tacc_job(experiment)
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')
    tacc_job_filepath = os.path.join(tacc_directory,f'{experiment}.sh')

    for d, p in zip([work_order,tacc_job],[work_order_filepath,tacc_job_filepath]):
        with open(p,'w') as f:
            f.write(d)

def set_reward_design_work_order(experiment):
    kwargs = preliminary_setup()
    schema = kwargs['schema']
    misc_directory = kwargs['misc_directory']
    schema_directory = kwargs['schema_directory']
    src_directory = kwargs['src_directory']
    work_order_directory = kwargs['work_order_directory']
    tacc_directory = kwargs['tacc_directory']
    settings = get_settings()
    train_buildings = settings['design_buildings']
    
    # buildings to include
    for building in schema['buildings']:
        schema['buildings'][building]['include'] = True if int(building.split('_')[-1]) in train_buildings else False

    grid_list = []

    simulation_end_time_step = {
        'reward_design_1': settings['test_end_time_step'],
        'reward_design_3': settings['train_end_time_step'],
    }

    schema['simulation_end_time_step'] = simulation_end_time_step[experiment]
    schema['agent']['attributes']['deterministic_start_time_step'] = (schema['simulation_end_time_step'] + 1)*(schema['episodes'] - 1)

    # reward definition
    for grid in settings['experiments']['reward_design']['grid']:
        param_names = list(grid.keys())
        param_values = list(grid.values())
        param_values_grid = list(itertools.product(*param_values))
        grid = pd.DataFrame(param_values_grid,columns=param_names)
        grid_list.append(grid)
    
    grid = pd.concat(grid_list,ignore_index=True,sort=True)
    grid['group'] = grid.index
    grid_list = []

    for seed in settings['seeds']:
        grid['seed'] = seed
        grid_list.append(grid.copy())

    grid = pd.concat(grid_list,ignore_index=True)
    grid = grid.sort_values(['type','seed','electricity_price_weight','electricity_price_exponent'])
    grid['buildings'] = str(train_buildings)
    grid['simulation_id'] = grid.reset_index().index.map(lambda x: f'{experiment}_{x}')
    grid.to_csv(os.path.join(misc_directory,f'{experiment}_grid.csv'),index=False)

    # design work order
    work_order = []

    for i, params in enumerate(grid.to_records(index=False)):
        schema['reward_function'] = {
            'type': params['type'],
            'attributes': {
                'electricity_price_weight': float(params['electricity_price_weight']),
                'carbon_emission_weight': float(1.0 - params['electricity_price_weight']),
                'electricity_price_exponent': float(params['electricity_price_exponent']),
                'carbon_emission_exponent': float(params['carbon_emission_exponent']),
            }  
        }
        schema['agent']['attributes']['seed'] = int(params['seed'])
        schema_filepath = os.path.join(schema_directory,f'{params["simulation_id"]}.json')
        write_json(schema_filepath, schema)
        work_order.append(f'python "{os.path.join(src_directory,"simulate.py")}" "{schema_filepath}" {params["simulation_id"]}')

    # write work order and tacc job
    work_order.append('')
    work_order = '\n'.join(work_order)
    tacc_job = get_tacc_job(experiment)
    work_order_filepath = os.path.join(work_order_directory,f'{experiment}.sh')
    tacc_job_filepath = os.path.join(tacc_directory,f'{experiment}.sh')

    for d, p in zip([work_order,tacc_job],[work_order_filepath,tacc_job_filepath]):
        with open(p,'w') as f:
            f.write(d)

def get_tacc_job(experiment, nodes=None):
    settings = get_settings()
    queue = settings['tacc_queue']['active']
    nodes = settings['tacc_queue']['metadata'][queue]['nodes'] if nodes is None else min(settings['tacc_queue']['metadata'][queue]['nodes'], nodes)
    nodes = int(nodes)
    time = settings['tacc_queue']['metadata'][queue]['time']
    kwargs = preliminary_setup()
    root_directory = kwargs['root_directory']
    log_directory = kwargs['log_directory']
    work_order_directory = kwargs['work_order_directory']
    log_filepath = os.path.join(log_directory,f'slurm_{experiment}.out')
    job_file = os.path.join(work_order_directory,f'{experiment}.sh')
    python_env = os.path.join(root_directory,'env','bin','activate')
    return '\n'.join([
        '#!/bin/bash',
        f'#SBATCH -p {queue}',
        f'#SBATCH -J citylearn_buildsys_2022_{experiment}',
        f'#SBATCH -N {nodes}',
        '#SBATCH --tasks-per-node 1',
        f'#SBATCH -t {time}',
        '#SBATCH --mail-user=nweye@utexas.edu',
        '#SBATCH --mail-type=all',
        f'#SBATCH -o {log_filepath}',
        '#SBATCH -A DemandAnalysis',
        '',
        '# load modules',
        'module load launcher',
        '',
        '# activate virtual environment',
        f'source {python_env}',
        '',
        '# set launcher environment variables',
        f'export LAUNCHER_WORKDIR="{root_directory}"',
        f'export LAUNCHER_JOB_FILE="{job_file}"',
        '',
        '${LAUNCHER_DIR}/paramrun',
    ])

def preliminary_setup():
    settings = get_settings()

    # set filepaths and directories
    src_directory = os.path.join(ROOT_DIRECTORY,'src')
    job_directory = os.path.join(ROOT_DIRECTORY,'job')
    log_directory = os.path.join(ROOT_DIRECTORY,'log')

    tacc_directory = os.path.join(job_directory,'tacc')
    work_order_directory = os.path.join(job_directory,'work_order')
    data_set_directory = os.path.join(DATA_DIRECTORY,settings['dataset_name'])
    schema_directory = os.path.join(DATA_DIRECTORY,'schema')
    misc_directory = os.path.join(DATA_DIRECTORY,'misc')
    result_directory = os.path.join(DATA_DIRECTORY,'result')
    agent_directory = os.path.join(DATA_DIRECTORY,'agent')
    summary_directory = os.path.join(DATA_DIRECTORY,'summary')
    database_directory = os.path.join(DATA_DIRECTORY,'database')
    figure_directory = os.path.join(ROOT_DIRECTORY,'figures')

    os.makedirs(schema_directory,exist_ok=True)
    os.makedirs(work_order_directory,exist_ok=True)
    os.makedirs(misc_directory,exist_ok=True)
    os.makedirs(tacc_directory,exist_ok=True)
    os.makedirs(log_directory,exist_ok=True)
    os.makedirs(result_directory,exist_ok=True)
    os.makedirs(agent_directory,exist_ok=True)
    os.makedirs(summary_directory,exist_ok=True)
    os.makedirs(database_directory,exist_ok=True)
    os.makedirs(figure_directory,exist_ok=True)

    # general simulation settings
    schema = read_json(os.path.join(data_set_directory,'schema.json'))
    schema['simulation_start_time_step'] = settings["train_start_time_step"]
    schema['simulation_end_time_step'] = settings["train_end_time_step"]
    schema['episodes'] = settings["train_episodes"]
    schema['root_directory'] = data_set_directory
    # set active observations
    for o in schema['observations']:
        schema['observations'][o]['active'] = True if o in settings['observations'] else False

    # define agent
    schema['agent'] = settings['default_agent']
    # set reward
    schema['reward_function'] = settings['default_reward_function']

    return {
        'schema': schema, 
        'root_directory': ROOT_DIRECTORY, 
        'src_directory': src_directory, 
        'schema_directory': schema_directory, 
        'work_order_directory': work_order_directory, 
        'misc_directory': misc_directory,
        'tacc_directory': tacc_directory,
        'log_directory': log_directory,
        'result_directory': result_directory,
        'summary_directory': summary_directory,
        'database_directory': database_directory,
        'figure_directory': figure_directory,
        'agent_directory': agent_directory,
    }

def set_work_order(experiment, **kwargs):
    set_dataset()
    func = {
        'reward_design_1':set_reward_design_work_order,
        'reward_design_3':set_reward_design_work_order,
        'hyperparameter_design_1':set_hyperparameter_design_work_order,
        'hyperparameter_design_3':set_hyperparameter_design_work_order,
        'rbc_validation':set_rbc_validation_work_order,
        'rbc_reference_1':set_rbc_reference_work_order,
        'rbc_reference_3':set_rbc_reference_work_order,
        'deployment_strategy_1_0':set_deployment_strategy_work_order,
        'deployment_strategy_1_1':set_deployment_strategy_work_order,
        'deployment_strategy_2_0':set_deployment_strategy_work_order,
        'deployment_strategy_3_0':set_deployment_strategy_work_order,
        'deployment_strategy_3_1':set_deployment_strategy_work_order,
        'deployment_strategy_3_2':set_deployment_strategy_work_order,
    }[experiment]
    func(experiment, **kwargs)

def get_experiments():
    return [
        'reward_design_1',
        'reward_design_3',
        'hyperparameter_design_1',
        'hyperparameter_design_3',
        'rbc_validation',
        'rbc_reference_1',
        'rbc_reference_3',
        'deployment_strategy_1_0',
        'deployment_strategy_1_1',
        'deployment_strategy_2_0',
        'deployment_strategy_3_0',
        'deployment_strategy_3_1',
        'deployment_strategy_3_2',
    ]

def set_dataset():
    settings = get_settings()
    dataset_name = settings['dataset_name']
    destination_directory = os.path.join(DATA_DIRECTORY, dataset_name)

    if os.path.isdir(destination_directory):
        shutil.rmtree(destination_directory)
    else:
        pass

    DataSet.copy(dataset_name, destination_directory=DATA_DIRECTORY)

def get_settings():
    src_directory = os.path.join(*Path(os.path.dirname(__file__)).absolute().parts)
    settings_filepath = os.path.join(src_directory,'settings.json')
    settings = read_json(settings_filepath)
    return settings

def main():
    parser = argparse.ArgumentParser(prog='buildsys_2022_simulate',formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('experiment',choices=get_experiments(),type=str)
    subparsers = parser.add_subparsers(title='subcommands',required=True,dest='subcommands')
    
    # set work order
    subparser_set_work_order = subparsers.add_parser('set_work_order')
    subparser_set_work_order.set_defaults(func=set_work_order)

    # run work order
    subparser_run_work_order = subparsers.add_parser('run_work_order')
    subparser_run_work_order.add_argument('-e', '--virtual_environment_path', dest='virtual_environment_path')
    subparser_run_work_order.add_argument('-w', '--windows_system', action='store_true', dest='windows_system')
    subparser_run_work_order.set_defaults(func=run)

    # set result summary
    subparser_set_result_summary = subparsers.add_parser('set_result_summary')
    subparser_set_result_summary.add_argument('-d','--detailed',action='store_true',dest='detailed')
    subparser_set_result_summary.set_defaults(func=set_result_summary)
    
    args = parser.parse_args()
    arg_spec = inspect.getfullargspec(args.func)
    kwargs = {
        key:value for (key, value) in args._get_kwargs() 
        if (key in arg_spec.args or (arg_spec.varkw is not None and key not in ['func','subcommands']))
    }
    args.func(**kwargs)

if __name__ == '__main__':
    sys.exit(main())