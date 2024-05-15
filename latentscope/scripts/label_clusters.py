# Usage: ls-label <dataset_id> <text_column> <cluster_id> <model_id> <context>
import os
import re
import sys
import json
import time
import argparse

try:
    # Check if the runtime environment is a Jupyter notebook
    if 'ipykernel' in sys.modules and 'IPython' in sys.modules:
        from tqdm.notebook import tqdm
    else:
        from tqdm import tqdm
except ImportError as e:
    # Fallback to the standard console version if import fails
    from tqdm import tqdm

from latentscope.util import get_data_dir
from latentscope.models import get_chat_model

def chunked_iterable(iterable, size):
    """Yield successive chunks from an iterable."""
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]

def too_many_duplicates(line, threshold=10):
    word_count = {}
    if not line:
        return False
    words = str(line).split()
    for word in words:
        word_count[word] = word_count.get(word, 0) + 1
    return any(count > threshold for count in word_count.values())

def main():
    parser = argparse.ArgumentParser(description='Label a set of slides using OpenAI')
    parser.add_argument('dataset_id', type=str, help='Dataset ID (directory name in data/)')
    parser.add_argument('text_column', type=str, help='Output file', default='text')
    parser.add_argument('cluster_id', type=str, help='ID of cluster set', default='cluster-001')
    parser.add_argument('model_id', type=str, help='ID of model to use', default="openai-gpt-3.5-turbo")
    parser.add_argument('context', type=str, help='Additional context for labeling model', default="")
    parser.add_argument('--rerun', type=str, help='Rerun the given embedding from last completed batch')

    # Parse arguments
    args = parser.parse_args()

    labeler(args.dataset_id, args.text_column, args.cluster_id, args.model_id, args.context, args.rerun)


def labeler(dataset_id, text_column="text", cluster_id="cluster-001", model_id="openai-gpt-3.5-turbo", context="", rerun=""):
    import numpy as np
    import pandas as pd
    DATA_DIR = get_data_dir()
    df = pd.read_parquet(os.path.join(DATA_DIR, dataset_id, "input.parquet"))

    # Load the indices for each cluster from the prepopulated labels file generated by cluster.py
    cluster_dir = os.path.join(DATA_DIR, dataset_id, "clusters")
    clusters = pd.read_parquet(os.path.join(cluster_dir, f"{cluster_id}-labels-default.parquet"))
    # initialize the labeled property to false when loading default clusters
    clusters = clusters.copy()
    clusters['labeled'] = False

    unlabeled_row = 0
    if rerun is not None:
        label_id = rerun
        clusters = pd.read_parquet(os.path.join(cluster_dir, f"{label_id}.parquet"))
        # print(clusters.columns)
        # find the first row where labeled isnt True
        unlabeled_row = clusters[~clusters['labeled']].first_valid_index()
        tqdm.write(f"First unlabeled row: {unlabeled_row}")
        

    else:
        # Determine the label id for the given cluster_id by checking existing label files
        label_files = [f for f in os.listdir(cluster_dir) if re.match(rf"{re.escape(cluster_id)}-labels-\d+\.parquet", f)]
        if label_files:
            # Extract label numbers and find the maximum
            label_numbers = [int(re.search(rf"{re.escape(cluster_id)}-labels-(\d+)\.parquet", f).group(1)) for f in label_files]
            next_label_number = max(label_numbers) + 1
        else:
            next_label_number = 1
        label_id = f"{cluster_id}-labels-{next_label_number:03d}"
    tqdm.write(f"RUNNING: {label_id}")

    model = get_chat_model(model_id)
    model.load_model()
    enc = model.encoder

    system_prompt = {"role":"system", "content": f"""You're job is to summarize lists of items with a short label of no more than 4 words. The items are part of a cluster and the label will be used to distinguish this cluster from others, so pay attention to what makes this group of similar items distinct.
{context}
The user will submit a bulleted list of items and you should choose a label that best summarizes the theme of the list so that someone browsing the labels will have a good idea of what is in the list. 
Do not use punctuation, Do not explain yourself, respond with only a few words that summarize the list."""}

    # TODO: why the extra 10 for openai?
    max_tokens = model.params["max_tokens"] - len(enc.encode(system_prompt["content"])) - 10

    # Create the lists of items we will send for summarization
    # Current looks like:
    # 1. item 1
    # 2. item 2
    # ...
    # we truncate the list based on tokens and we also remove items that have too many duplicate words
    extracts = []
    for _, row in clusters.iterrows():
        indices = row['indices']
        items = df.loc[list(indices), text_column]
        items = items.drop_duplicates()
        text = '\n'.join([f"{i+1}. {t}" for i, t in enumerate(items) if not too_many_duplicates(t)])
        encoded_text = enc.encode(text)
        if len(encoded_text) > max_tokens:
            encoded_text = encoded_text[:max_tokens]
        extracts.append(enc.decode(encoded_text))

    # TODO we arent really batching these
    batch_size = 1
    labels = []
    clean_labels = []


    for i,batch in enumerate(tqdm(chunked_iterable(extracts, batch_size),  total=len(extracts)//batch_size)):
        # tqdm.write(batch[0])
        if(unlabeled_row > 0):
            if clusters.loc[i, 'labeled']:
                tqdm.write(f"skipping {i} already labeled {clusters.loc[i, 'label']}")
                time.sleep(0.01)
                continue

        try:
            time.sleep(0.01)
            messages=[
                system_prompt, {"role":"user", "content": "Here is a list of items, please summarize the list into a label using only a few words:\n" + batch[0]} # TODO hardcoded batch size
            ]
            label = model.chat(messages)
            labels.append(label)
            # tqdm.write("label:\n", label)
            # do some cleanup of the labels when the model doesn't follow instructions
            clean_label = label.replace("\n", " ")
            clean_label = clean_label.replace('"', '')
            clean_label = clean_label.replace("'", '')
            # clean_label = clean_label.replace("-", '')
            clean_label = ' '.join(clean_label.split())
            clean_label = " ".join(clean_label.split(" ")[0:5])
            clean_labels.append(clean_label)
            
            tqdm.write(f"cluster {i} label: {clean_label}")
            clusters.loc[i, 'label'] = clean_label
            clusters.loc[i, 'label_raw'] = label
            clusters.loc[i, 'labeled'] = True
            # length = len(clean_labels) - 1
            # clusters_df.loc[unlabled_row:unlabled_row+length, 'label'] = clean_labels
            # clusters_df.loc[unlabled_row:unlabled_row+length, 'label_raw'] = labels
            # clusters_df.loc[unlabled_row:unlabled_row+length, 'labeled'] = [True for i in range(0, len(labels))]
            clusters.to_parquet(os.path.join(cluster_dir, f"{label_id}.parquet"))
            # update 

        except Exception as e: 
            tqdm.write(f"{batch[0]}")
            tqdm.write(f"ERROR: {e}")
            tqdm.write("exiting")
            exit(1)

    print("labels:", len(labels))
    # add lables to slides df

    # write the df to parquet
    with open(os.path.join(cluster_dir,f"{label_id}.json"), 'w') as f:
        json.dump({
            "id": label_id,
            "cluster_id": cluster_id,
            "model_id": model_id, 
            "text_column": text_column,
            "context": context,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
        }, f, indent=2)
    f.close()
    print("done with", label_id)

if __name__ == "__main__":
    main()
