# 메시지 베이스 형식과 편리 함수들을 정의함.
import json
import common.utils


class _4DMsg:
    REQUEST = 'request'
    RESPONSE = 'response'
    NOTIFY = 'notify'
    
    # 파이썬에서 디폴트 파라미터는 정의될 때 한번만 평가됨.
    # 따라서 디폴트 파라미터로 utils.generate_token() 을 사용하면 항상 같은 랜덤 문자열이 생성됨.
    def __init__(self,_sec1: str='', _sec2: str='', _sec3: str='', _from: str='', _to: str='', _state: str=''):
        self.data = {
            'Section1':_sec1,
            'Section2':_sec2,
            'Section3':_sec3,
            'From':_from,
            'To':_to,
            'SendState':_state,
            'Token':v4.utils.generate_token(),
        }
        self.format_err = None  # 메시지 형식을 완성할 수 없음
        
        
    # dict 형태의 파라미터를 수신하여 data 에 업데이트 함.
    #   ex) obj.update(cameras=['10.1.1.1','10.1.1.2])  -> cameras key 추가
    #   ex) obj.update(Section3='New Section3')         -> Section3 변경
    #   ex) obj.update(**dict)                          -> idct 추가 (같은 key 가 존재할 경우 값이 갱신됨)
    def update(self, **kwargs):
        self.data.update(kwargs)
        
        
    # json format 문자열 리턴
    def get_json(self) -> tuple[bool,str]:
        ret, string = True, ''
        try:
            string = json.dumps(self.data)
        except TypeError as e:
            ret, string = False, str(e)
        return ret, string
    
    
    # msg 유효성 검사
    def is_valid(self) -> bool:
        # Section 1,2,3 가 존재하지 않을 경우
        if 'Section1' not in self.data      \
            or 'Section2' not in self.data  \
            or 'Section3' not in self.data: \
                return False
        # 키에 해당하는 값이 존재하지 않거나 공백 문자열일 경우
        # isspace() 함수는 '' 을 space 로 간주하지 않기 때문에 strip+len 으로 검사함
        if len(self.data.get('From','').strip()) == 0            \
            or len(self.data.get('To','').strip()) == 0          \
            or len(self.data.get('SendState','').strip()) == 0   \
            or len(self.data.get('Token','').strip()) == 0:
                return False
        return True
    
    
    # 문자열(메시지)을 할당
    def assign(self, msg: str) -> bool:
        try:
            self.data.update(json.loads(msg))
        except json.JSONDecodeError as e:
            print("JSON 파싱 오류:", e)
        except TypeError as e:
            print("타입 에러:", e)
        return self.is_valid()
    
    
    # key 에 해당하는 값을 리턴
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    
    # requset -> response
    # response -> rqeuset
    # swap from and to
    def toggle_status(self):
        self.data['SendState'] = _4DMsg.REQUEST if self.data['SendState'] == _4DMsg.RESPONSE else _4DMsg.RESPONSE
        self.data['From'], self.data['To'] = self.data['To'], self.data['From']


if __name__ == "__main__":
    msg = '''{
	"Section1": "sec1",
	"Section2": "sec2",
	"Section3": "sec3",
	"From": "from",
	"To": "to",
	"SendState": "state",
	"Token": "token"
    }'''
    # msg = '[1,2,3,4,5]'   # 잘못된 형식
    _4dmsg = _4DMsg()
    ret = _4dmsg.assign(msg)
    print(_4dmsg.get_json())
