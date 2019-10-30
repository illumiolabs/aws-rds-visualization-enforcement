#   Copyright 2019 Illumio, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import os
from io import BytesIO
from gzip import GzipFile
from botocore.vendored import requests
import boto3
from urllib.parse import unquote_plus
import traceback


# PCE API request call using requests module
def pce_request(pce, org_id, key, secret, verb, path, params=None,
                data=None, json=None, extra_headers=None):
    base_url = os.path.join(pce, 'orgs', org_id)
    headers = {
              'user-agent': 'aws-rds-flow-viz',
            }
    print(base_url)
    if extra_headers:
        headers.update(extra_headers)
        print(headers)
    if json:
        print(json)
    response = requests.request(verb,
                                os.path.join(base_url, path),
                                auth=(key, secret),
                                headers=headers,
                                params=params,
                                json=json,
                                data=data)
    return response


def get_flow_logs_from_s3(event):
    flows = " "
    headers = {
        'Content-Encoding': 'deflate',
        'X-Bulk-Traffic-Load-CSV-Version': '1',
        'Content-Type': "text/csv"
    }
    pce_api = int(os.environ['ILO_API_VERSION'])
    pce = os.path.join('https://'+os.environ['ILLUMIO_SERVER']+':'+os.environ['ILO_PORT'], 'api', 'v%d' % pce_api)
    org_id = os.environ['ILO_ORG_ID']
    api_key = 'api_'+os.environ['ILO_API_KEY_ID']
    secret = os.environ['ILO_API_KEY_SECRET']
    traffic_flows = 'agents/bulk_traffic_flows'
    try:
        s3 = boto3.resource(u's3')
        s3 = boto3.client('s3')
        for record in event['Records']:
            print(record)
            filename = record['s3']['object']['key']
            filesize = record['s3']['object']['size']
            source = record['requestParameters']['sourceIPAddress']
            eventTime = record['eventTime']
            print(filename, filesize, source, eventTime)
        # get a handle on the bucket that holds your file
        bucket_name = record['s3']['bucket']['name']
        # get a handle on the object you want (i.e. your file)
        key = unquote_plus(record['s3']['object']['key'])
        data = s3.get_object(Bucket=bucket_name, Key=key)
        bytestream = BytesIO(data['Body'].read())
        got_text = GzipFile(None, 'rb', fileobj=bytestream).read().decode('utf-8')
        flow_logs = got_text.split('\n')
        for flow in flow_logs:
            flow_data = flow.split(" ")
            if len(flow_data) >= 4:
                # see flags guide as mentioned here
                # https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html
                if flow_data[4] == '2' or flow_data[4] == '3' or flow_data[4] == '19':
                    flows += ("%s,%s,%s,%s\n" % (flow_data[0], flow_data[1], flow_data[2], flow_data[3]))
        # Getting the data from environment variables for the PCE API request
        print('The individual flows sent to the PCE are ')
        print(flows)
        response = pce_request(pce, org_id, api_key, secret, 'POST', traffic_flows, data=flows, extra_headers=headers).json()
        print(response)
        return response
    except Exception as e:
        print(traceback.format_exc())
        status_code = 500
        print(e)
        return status_code


def lambda_handler(event, context):
    flows = get_flow_logs_from_s3(event)
    return {
        'statusCode': 'success',
        'body': flows
    }
